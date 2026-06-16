"""Fibey Coordinator — MAF AgentRuntime with streaming ResponseEventStream.

Network operations coordinator agent that monitors telemetry, dispatches work orders,
tracks incidents, and saves investigations. Uses real file persistence via HOME directory.

Uses MAF (Microsoft Agent Framework) AgentRuntime with FoundryChatClient
for proper tool orchestration, and ResponseEventStream for streaming output.
"""

import asyncio
import json
import logging
import os
import pathlib
import re
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(override=False)

from azure.identity import DefaultAzureCredential
from azure.ai.agentserver.responses import (
    ResponseContext,
    ResponseEventStream,
    ResponsesAgentServerHost,
    ResponsesServerOptions,
    get_input_expanded,
)
from azure.ai.agentserver.responses.models import CreateResponse
from agent_framework import FunctionTool
from agent_framework_foundry import FoundryChatClient

from agent_framework.observability import enable_instrumentation
from toolbox import build_toolbox_tool

enable_instrumentation(enable_sensitive_data=True)

# ── Agent name and logger ────────────────────────────────────────────────────


def _read_agent_name() -> str:
    try:
        yaml_text = pathlib.Path("agent.yaml").read_text()
        m = re.search(r"^name:\s*(.+)$", yaml_text, re.MULTILINE)
        return m.group(1).strip() if m else "fibey-coordinator"
    except Exception:
        return "fibey-coordinator"


AGENT_NAME = _read_agent_name()
logger = logging.getLogger(AGENT_NAME)

# ── Configuration ─────────────────────────────────────────────────────────────

PROJECT_ENDPOINT = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "")
if not PROJECT_ENDPOINT:
    raise ValueError("FOUNDRY_PROJECT_ENDPOINT must be set")

MODEL_DEPLOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME", "") or os.getenv(
    "AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4.1"
)

# Home directory for file persistence
HOME_DIR = pathlib.Path(os.environ.get("HOME", "/tmp"))
INVESTIGATIONS_DIR = HOME_DIR / "investigations"

# ── Mock Data ─────────────────────────────────────────────────────────────────

MOCK_TELEMETRY = {
    "quincy_north": {
        "site": "Quincy North",
        "overall_status": "degraded",
        "alerts": [
            {
                "id": "ALT-20260524-001",
                "severity": "critical",
                "source": "Rack B-14, Port 3/1/12",
                "type": "optical_power_low",
                "message": "Rx optical power -18.2 dBm (threshold: -14 dBm). CRC error rate 2.3e-6.",
                "first_seen": "2026-05-24T07:45:00Z",
                "acknowledged": False,
            },
            {
                "id": "ALT-20260524-002",
                "severity": "warning",
                "source": "Row A Panel 7",
                "type": "temperature_elevated",
                "message": "Ambient temperature 28.4C (warning threshold: 27C). HVAC unit 3 degraded.",
                "first_seen": "2026-05-24T09:30:00Z",
                "acknowledged": True,
            },
        ],
        "metrics": {
            "total_ports": 4608,
            "active_ports": 3847,
            "error_ports": 3,
            "avg_latency_ms": 0.42,
            "packet_loss_pct": 0.001,
            "uptime_pct": 99.97,
        },
    },
    "default": {
        "site": "All Sites",
        "overall_status": "healthy",
        "alerts": [],
        "metrics": {
            "total_ports": 12000,
            "active_ports": 9500,
            "error_ports": 3,
            "avg_latency_ms": 0.55,
            "packet_loss_pct": 0.0005,
            "uptime_pct": 99.99,
        },
    },
}

MOCK_INCIDENTS = [
    # ── P1 Critical (3 incidents) ────────────────────────────────────────────
    {
        "id": "INC-20260524-001",
        "title": "Optical link degradation Rack B-14",
        "severity": "P1",
        "status": "investigating",
        "type": "optical_degradation",
        "site": "Quincy North",
        "created": "2026-05-24T07:50:00Z",
        "last_update": "2026-05-25T06:15:00Z",
        "description": "Multiple CRC errors and low Rx power detected on port 3/1/12. Likely failed QSFP-DD transceiver. Field tech dispatched.",
        "assigned_to": "Field Tech On-Site",
        "timeline": [
            "07:45 - Alert triggered: Rx power below threshold",
            "07:50 - Incident auto-created",
            "08:00 - Acknowledged by NOC",
            "08:30 - Work order WO-20260524-001 dispatched",
            "12:00 - Field tech en route",
        ],
    },
    {
        "id": "INC-20260525-014",
        "title": "Fiber cut detected on inter-DC trunk SE-04",
        "severity": "P1",
        "status": "escalated",
        "type": "fiber_cut",
        "site": "Singapore South",
        "created": "2026-05-25T03:22:00Z",
        "last_update": "2026-05-25T11:40:00Z",
        "description": "Complete loss of signal on trunk SE-04 between Singapore South and Jakarta POP. Traffic rerouted via SE-02 (60% capacity). Vendor dispatched for emergency splice.",
        "assigned_to": "Regional Fiber Vendor (SingTel)",
        "timeline": [
            "03:22 - LOS alarm on trunk SE-04",
            "03:23 - Auto-failover to SE-02",
            "03:30 - NOC confirmed fiber cut via OTDR",
            "04:00 - Vendor notified — SLA 6h restore",
            "11:40 - Splice team on-site, ETA 2h to restore",
        ],
    },
    {
        "id": "INC-20260522-009",
        "title": "UPS bank failure — Generator on load",
        "severity": "P1",
        "status": "monitoring",
        "type": "power",
        "site": "East US DC-7",
        "created": "2026-05-22T14:18:00Z",
        "last_update": "2026-05-25T08:00:00Z",
        "description": "UPS Bank C failed during routine transfer test. Generator G-2 carrying load. Replacement UPS modules ordered — 48h ETA.",
        "assigned_to": "Critical Power Team",
        "timeline": [
            "14:18 - UPS Bank C offline",
            "14:19 - Generator G-2 auto-started",
            "14:25 - NOC confirmed stable on generator",
            "15:00 - Vendor notified for replacement modules",
            "May 25 08:00 - Modules in transit, ETA May 26 AM",
        ],
    },
    # ── P2 High (6 incidents) ────────────────────────────────────────────────
    {
        "id": "INC-20260523-003",
        "title": "HVAC Unit 3 degraded performance",
        "severity": "P2",
        "status": "waiting-for-vendor",
        "type": "cooling",
        "site": "Quincy North",
        "created": "2026-05-23T16:00:00Z",
        "last_update": "2026-05-25T09:30:00Z",
        "description": "HVAC Unit 3 running at 70% capacity. Ambient temp elevated but within operational limits. Vendor dispatched for Monday.",
        "assigned_to": "Facilities Team",
        "timeline": [
            "May 23 16:00 - Performance degradation detected",
            "May 23 16:30 - Vendor notified",
            "May 24 09:30 - Temperature warning threshold crossed in Row A",
            "May 25 09:30 - Vendor confirms Monday AM arrival",
        ],
    },
    {
        "id": "INC-20260524-005",
        "title": "Elevated packet loss on spine switch SW-SPINE-03",
        "severity": "P2",
        "status": "investigating",
        "type": "optical_degradation",
        "site": "Dublin West",
        "created": "2026-05-24T11:05:00Z",
        "last_update": "2026-05-25T07:20:00Z",
        "description": "Intermittent packet loss (0.02%) on SW-SPINE-03 uplinks. Correlates with optics temperature. Suspecting failing fan tray.",
        "assigned_to": "Network Engineering",
        "timeline": [
            "11:05 - Packet loss threshold exceeded",
            "11:20 - Correlated with optics temp spike",
            "13:00 - Fan tray inspection scheduled",
            "May 25 07:20 - Fan 3 confirmed degraded, RMA filed",
        ],
    },
    {
        "id": "INC-20260523-007",
        "title": "Capacity planning — Quincy North nearing 85% port utilization",
        "severity": "P2",
        "status": "monitoring",
        "type": "capacity_planning",
        "site": "Quincy North",
        "created": "2026-05-23T09:00:00Z",
        "last_update": "2026-05-25T06:00:00Z",
        "description": "Port utilization trending at 83.5% and climbing. Need to plan leaf expansion in rows C-D within 30 days to avoid congestion.",
        "assigned_to": "Capacity Planning",
        "timeline": [
            "May 23 09:00 - Monthly capacity report flagged threshold",
            "May 23 14:00 - Expansion options reviewed",
            "May 25 06:00 - Procurement ticket submitted for 48-port leaf switches x8",
        ],
    },
    {
        "id": "INC-20260524-008",
        "title": "PDU phase imbalance Hall B",
        "severity": "P2",
        "status": "investigating",
        "type": "power",
        "site": "East US DC-7",
        "created": "2026-05-24T19:44:00Z",
        "last_update": "2026-05-25T10:15:00Z",
        "description": "Phase C drawing 38% more current than phases A/B on PDU-B-22. Risk of breaker trip if load increases. Load rebalancing in progress.",
        "assigned_to": "Critical Power Team",
        "timeline": [
            "19:44 - Phase imbalance alert triggered",
            "20:00 - NOC acknowledged",
            "May 25 10:15 - Load migration started from Phase C",
        ],
    },
    {
        "id": "INC-20260521-011",
        "title": "BGP session flapping with upstream peer AS7018",
        "severity": "P2",
        "status": "resolved",
        "type": "optical_degradation",
        "site": "Dublin West",
        "created": "2026-05-21T22:15:00Z",
        "last_update": "2026-05-24T16:00:00Z",
        "description": "BGP session to AT&T peer (AS7018) flapping every ~4h. Root cause: MTU mismatch after peer-side maintenance. Resolved with peer NOC.",
        "assigned_to": "Peering Team",
        "timeline": [
            "22:15 - BGP session down alarm",
            "22:16 - Session restored",
            "May 22 02:00 - Second flap detected",
            "May 22 10:00 - Pattern identified — MTU mismatch",
            "May 24 16:00 - Peer confirmed fix deployed, stable since",
        ],
    },
    {
        "id": "INC-20260525-016",
        "title": "Cooling tower water flow sensor fault",
        "severity": "P2",
        "status": "waiting-for-vendor",
        "type": "cooling",
        "site": "Sweden Central",
        "created": "2026-05-25T05:10:00Z",
        "last_update": "2026-05-25T12:00:00Z",
        "description": "Flow sensor CT-3 reporting 0 L/min despite pump running. Likely sensor failure, not actual flow loss. Vendor dispatched for replacement.",
        "assigned_to": "Facilities Team",
        "timeline": [
            "05:10 - Zero-flow alarm on CT-3",
            "05:15 - Manual verification confirmed pump running",
            "05:30 - Sensor fault confirmed",
            "12:00 - Vendor ETA tomorrow 08:00 local",
        ],
    },
    # ── P3 Normal (15 incidents) ─────────────────────────────────────────────
    {
        "id": "INC-20260524-002",
        "title": "Optics inventory low — 100G QSFP28 SR4",
        "severity": "P3",
        "status": "monitoring",
        "type": "capacity_planning",
        "site": "Quincy North",
        "created": "2026-05-24T08:00:00Z",
        "last_update": "2026-05-25T06:00:00Z",
        "description": "Only 12 spare 100G QSFP28 SR4 remaining (threshold: 20). Reorder placed, 5-day lead time.",
        "assigned_to": "Supply Chain",
        "timeline": [
            "08:00 - Inventory threshold alert",
            "09:00 - Purchase order submitted",
            "May 25 - Shipment confirmed, ETA May 29",
        ],
    },
    {
        "id": "INC-20260523-004",
        "title": "Scheduled firmware upgrade — Leaf switches Row E",
        "severity": "P3",
        "status": "monitoring",
        "type": "capacity_planning",
        "site": "East US DC-7",
        "created": "2026-05-23T10:00:00Z",
        "last_update": "2026-05-25T02:00:00Z",
        "description": "Rolling firmware upgrade for 16 leaf switches in Row E. 12/16 complete. Next maintenance window tonight 02:00-06:00 UTC.",
        "assigned_to": "Network Engineering",
        "timeline": [
            "May 23 - Change approved (CR-4412)",
            "May 24 02:00 - First batch (8 switches) upgraded",
            "May 25 02:00 - Second batch (4 switches) upgraded",
            "Tonight - Final 4 switches scheduled",
        ],
    },
    {
        "id": "INC-20260522-006",
        "title": "DNS resolution latency spike from DC-7",
        "severity": "P3",
        "status": "resolved",
        "type": "optical_degradation",
        "site": "East US DC-7",
        "created": "2026-05-22T03:45:00Z",
        "last_update": "2026-05-23T14:00:00Z",
        "description": "DNS queries from DC-7 seeing 15ms latency (normal <2ms). Traced to stale route to DNS anycast. Flushed and stable.",
        "assigned_to": "Network Engineering",
        "timeline": [
            "03:45 - Latency alert triggered",
            "04:00 - Stale route identified",
            "04:05 - Route flushed, latency normalized",
            "May 23 14:00 - 24h stable, closing",
        ],
    },
    {
        "id": "INC-20260524-010",
        "title": "Badge reader malfunction — Loading Dock 2",
        "severity": "P3",
        "status": "waiting-for-vendor",
        "type": "power",
        "site": "Dublin West",
        "created": "2026-05-24T13:30:00Z",
        "last_update": "2026-05-25T09:00:00Z",
        "description": "Badge reader at Loading Dock 2 intermittently failing. Security using manual log. Vendor scheduled for Tuesday.",
        "assigned_to": "Physical Security",
        "timeline": [
            "13:30 - Reported by security guard",
            "14:00 - Fallback to manual entry log",
            "May 25 09:00 - Vendor confirmed Tuesday slot",
        ],
    },
    {
        "id": "INC-20260525-012",
        "title": "Fiber patch panel labeling discrepancy Row F",
        "severity": "P3",
        "status": "investigating",
        "type": "fiber_cut",
        "site": "Quincy North",
        "created": "2026-05-25T08:30:00Z",
        "last_update": "2026-05-25T10:00:00Z",
        "description": "Field tech reported 6 mismatched labels on patch panel F-12 during routine audit. No service impact. Audit and relabeling scheduled.",
        "assigned_to": "Cabling Team",
        "timeline": [
            "08:30 - Discrepancy reported during audit",
            "10:00 - Full panel audit scheduled for this week",
        ],
    },
    {
        "id": "INC-20260521-013",
        "title": "CRAC unit condensation alert — Row G",
        "severity": "P3",
        "status": "resolved",
        "type": "cooling",
        "site": "Singapore South",
        "created": "2026-05-21T04:00:00Z",
        "last_update": "2026-05-23T10:00:00Z",
        "description": "Condensation sensor triggered on CRAC unit G-2. Humidity setpoint adjusted. No equipment damage.",
        "assigned_to": "Facilities Team",
        "timeline": [
            "04:00 - Condensation alert",
            "04:30 - Humidity setpoint lowered from 55% to 48%",
            "May 23 10:00 - 48h stable, closing",
        ],
    },
    {
        "id": "INC-20260524-015",
        "title": "NTP sync drift on management switches",
        "severity": "P3",
        "status": "resolved",
        "type": "optical_degradation",
        "site": "Sweden Central",
        "created": "2026-05-24T06:12:00Z",
        "last_update": "2026-05-24T18:00:00Z",
        "description": "Management plane switches drifted 340ms from stratum-1. NTP source failover triggered. Stable after primary source recovered.",
        "assigned_to": "Network Engineering",
        "timeline": [
            "06:12 - NTP drift alarm",
            "06:13 - Failover to secondary NTP source",
            "12:00 - Primary source recovered",
            "18:00 - All switches synced within 1ms, closing",
        ],
    },
    {
        "id": "INC-20260523-017",
        "title": "Elevated error rate on 400G coherent optics — Span DUB-LON",
        "severity": "P3",
        "status": "monitoring",
        "type": "optical_degradation",
        "site": "Dublin West",
        "created": "2026-05-23T20:45:00Z",
        "last_update": "2026-05-25T08:00:00Z",
        "description": "Pre-FEC BER elevated on Dublin-London span. Within correction margin but trending. Monitoring for 72h before fiber cleaning.",
        "assigned_to": "Optical Engineering",
        "timeline": [
            "20:45 - Pre-FEC BER alarm (1.2e-3, threshold 1e-3)",
            "21:00 - Confirmed post-FEC error-free",
            "May 25 08:00 - Still elevated but stable. Fiber clean scheduled May 27",
        ],
    },
    {
        "id": "INC-20260525-018",
        "title": "Smoke detector test — Hall C scheduled maintenance",
        "severity": "P3",
        "status": "monitoring",
        "type": "power",
        "site": "East US DC-7",
        "created": "2026-05-25T07:00:00Z",
        "last_update": "2026-05-25T07:30:00Z",
        "description": "Scheduled quarterly smoke detector testing in Hall C. VESDA suppressed for duration. Expected completion by 11:00 UTC.",
        "assigned_to": "Facilities Team",
        "timeline": [
            "07:00 - Maintenance window opened",
            "07:30 - Testing in progress, zones 1-4 of 12 complete",
        ],
    },
    {
        "id": "INC-20260522-019",
        "title": "Link utilization spike — Customer peering port",
        "severity": "P3",
        "status": "resolved",
        "type": "capacity_planning",
        "site": "Singapore South",
        "created": "2026-05-22T15:00:00Z",
        "last_update": "2026-05-23T09:00:00Z",
        "description": "Customer ABC Corp peering port hit 92% utilization during content push. Temporary, resolved after push completed. LAG expansion discussed.",
        "assigned_to": "Peering Team",
        "timeline": [
            "15:00 - 90% utilization alarm",
            "15:30 - Confirmed planned content push by customer",
            "May 23 09:00 - Utilization returned to normal 45%. LAG upgrade on roadmap",
        ],
    },
    {
        "id": "INC-20260524-020",
        "title": "Backup generator monthly test — Gen G-4",
        "severity": "P3",
        "status": "resolved",
        "type": "power",
        "site": "Sweden Central",
        "created": "2026-05-24T10:00:00Z",
        "last_update": "2026-05-24T11:30:00Z",
        "description": "Monthly load test for Generator G-4 completed successfully. All parameters within spec.",
        "assigned_to": "Critical Power Team",
        "timeline": [
            "10:00 - Test initiated",
            "10:30 - Full load achieved",
            "11:30 - Test complete, all parameters nominal",
        ],
    },
    {
        "id": "INC-20260525-021",
        "title": "Rack power circuit near capacity — Rack D-22",
        "severity": "P3",
        "status": "investigating",
        "type": "power",
        "site": "Quincy North",
        "created": "2026-05-25T09:00:00Z",
        "last_update": "2026-05-25T11:30:00Z",
        "description": "Rack D-22 drawing 18.2kW on 20kW circuit (91%). New GPU node install pushed it near limit. Evaluating rebalance options.",
        "assigned_to": "Critical Power Team",
        "timeline": [
            "09:00 - 90% circuit utilization alarm",
            "09:30 - Traced to new GPU node installed May 24",
            "11:30 - Options: move 2 nodes to D-23 or upgrade circuit",
        ],
    },
    {
        "id": "INC-20260523-022",
        "title": "Cross-connect delivery delayed — MMR port allocation",
        "severity": "P3",
        "status": "waiting-for-vendor",
        "type": "capacity_planning",
        "site": "Dublin West",
        "created": "2026-05-23T11:00:00Z",
        "last_update": "2026-05-25T08:00:00Z",
        "description": "New cross-connect to carrier COLT delayed 3 days. Meet-me room port allocation pending facility operator.",
        "assigned_to": "Supply Chain",
        "timeline": [
            "May 23 11:00 - Cross-connect order placed",
            "May 24 - Facility operator confirmed delay",
            "May 25 08:00 - New ETA: May 28",
        ],
    },
    # ── P4 Low (8 incidents) ─────────────────────────────────────────────────
    {
        "id": "INC-20260521-023",
        "title": "Cable management cleanup — Row A cabling",
        "severity": "P4",
        "status": "monitoring",
        "type": "capacity_planning",
        "site": "Quincy North",
        "created": "2026-05-21T12:00:00Z",
        "last_update": "2026-05-24T16:00:00Z",
        "description": "Post-expansion cable management in Row A needs tidying. Scheduled for next maintenance window. No operational impact.",
        "assigned_to": "Cabling Team",
        "timeline": [
            "May 21 - Flagged during walkthrough",
            "May 24 - Scheduled for May 27 maintenance window",
        ],
    },
    {
        "id": "INC-20260522-024",
        "title": "DCIM dashboard widget not refreshing — temperature view",
        "severity": "P4",
        "status": "resolved",
        "type": "cooling",
        "site": "East US DC-7",
        "created": "2026-05-22T09:00:00Z",
        "last_update": "2026-05-22T14:00:00Z",
        "description": "DCIM temperature dashboard widget stuck on stale data. Root cause: Redis cache TTL misconfigured after upgrade. Fixed.",
        "assigned_to": "DCIM Admin",
        "timeline": [
            "09:00 - Reported by NOC shift lead",
            "10:00 - Redis TTL issue identified",
            "14:00 - Fix deployed, dashboard live",
        ],
    },
    {
        "id": "INC-20260524-025",
        "title": "Documentation update — Singapore fiber map",
        "severity": "P4",
        "status": "investigating",
        "type": "fiber_cut",
        "site": "Singapore South",
        "created": "2026-05-24T02:00:00Z",
        "last_update": "2026-05-25T04:00:00Z",
        "description": "Fiber route map for Singapore South outdated after Q1 expansion. New paths not documented. Update in progress.",
        "assigned_to": "Documentation Team",
        "timeline": [
            "May 24 02:00 - Flagged during INC-20260525-014 response",
            "May 25 04:00 - First draft of updated map submitted for review",
        ],
    },
    {
        "id": "INC-20260523-026",
        "title": "Environmental monitoring sensor battery low — Zone 3",
        "severity": "P4",
        "status": "waiting-for-vendor",
        "type": "cooling",
        "site": "Sweden Central",
        "created": "2026-05-23T14:00:00Z",
        "last_update": "2026-05-25T06:00:00Z",
        "description": "Wireless temp/humidity sensor in Zone 3 reporting low battery (12%). Replacement batteries ordered.",
        "assigned_to": "Facilities Team",
        "timeline": [
            "14:00 - Low battery alert",
            "14:30 - Batteries ordered (3-day delivery)",
            "May 25 06:00 - Shipment tracking confirms May 26 delivery",
        ],
    },
    {
        "id": "INC-20260525-027",
        "title": "Decommission old DWDM shelf — Rack A-03",
        "severity": "P4",
        "status": "monitoring",
        "type": "capacity_planning",
        "site": "Dublin West",
        "created": "2026-05-25T06:00:00Z",
        "last_update": "2026-05-25T09:00:00Z",
        "description": "Legacy DWDM shelf in Rack A-03 fully migrated. Decom scheduled for next maintenance window. Power draw 800W that can be reclaimed.",
        "assigned_to": "Network Engineering",
        "timeline": [
            "06:00 - Final circuit migrated off shelf",
            "09:00 - Decom change request submitted (CR-4498)",
        ],
    },
    {
        "id": "INC-20260520-028",
        "title": "Annual fire suppression inspection — Hall A",
        "severity": "P4",
        "status": "resolved",
        "type": "power",
        "site": "Quincy North",
        "created": "2026-05-20T08:00:00Z",
        "last_update": "2026-05-22T17:00:00Z",
        "description": "Annual fire suppression system inspection completed. All systems passed. Certificate filed.",
        "assigned_to": "Facilities Team",
        "timeline": [
            "May 20 08:00 - Inspection started",
            "May 20 12:00 - All zones tested",
            "May 22 17:00 - Certificate received and filed",
        ],
    },
    {
        "id": "INC-20260524-029",
        "title": "Network TAP mirror port reconfiguration",
        "severity": "P4",
        "status": "resolved",
        "type": "capacity_planning",
        "site": "Singapore South",
        "created": "2026-05-24T04:30:00Z",
        "last_update": "2026-05-24T07:00:00Z",
        "description": "Security team requested TAP mirror port change for new IDS appliance. Completed during maintenance window.",
        "assigned_to": "Network Engineering",
        "timeline": [
            "04:30 - Change request received",
            "05:00 - Maintenance window opened",
            "07:00 - Mirror port reconfigured and verified",
        ],
    },
    {
        "id": "INC-20260525-030",
        "title": "LED status panel flickering — NOC wall display",
        "severity": "P4",
        "status": "investigating",
        "type": "power",
        "site": "East US DC-7",
        "created": "2026-05-25T10:00:00Z",
        "last_update": "2026-05-25T11:00:00Z",
        "description": "NOC wall display panel 3 flickering intermittently. Likely HDMI cable or display driver. Non-critical but annoying for NOC staff.",
        "assigned_to": "IT Support",
        "timeline": [
            "10:00 - Reported by NOC operator",
            "11:00 - HDMI cable replacement scheduled",
        ],
    },
    {
        "id": "INC-20260519-031",
        "title": "Quarterly optics spares audit complete",
        "severity": "P4",
        "status": "resolved",
        "type": "capacity_planning",
        "site": "Sweden Central",
        "created": "2026-05-19T09:00:00Z",
        "last_update": "2026-05-21T15:00:00Z",
        "description": "Quarterly audit of optics spares inventory. All SKUs within threshold except 400G ZR+ (flagged separately). Report filed.",
        "assigned_to": "Supply Chain",
        "timeline": [
            "May 19 09:00 - Audit initiated",
            "May 20 - Physical count completed",
            "May 21 15:00 - Report submitted, 400G ZR+ reorder triggered",
        ],
    },
    {
        "id": "INC-20260525-032",
        "title": "Chiller plant efficiency below target — Building 2",
        "severity": "P3",
        "status": "investigating",
        "type": "cooling",
        "site": "Quincy North",
        "created": "2026-05-25T07:00:00Z",
        "last_update": "2026-05-25T12:30:00Z",
        "description": "Chiller PUE contribution trending 0.08 above target. Likely due to warmer-than-expected ambient this week. Evaluating economizer mode adjustment.",
        "assigned_to": "Facilities Engineering",
        "timeline": [
            "07:00 - Weekly efficiency report flagged regression",
            "08:00 - Correlated with ambient temp spike (34C vs 28C seasonal avg)",
            "12:30 - Economizer setpoint adjustment under review",
        ],
    },
]


# ── Tool Implementations ─────────────────────────────────────────────────────


def check_network_telemetry(site: str = "", metric: str = "") -> str:
    """Check real-time network telemetry, alerts, and metrics for a site.

    Args:
        site: Site name to query (e.g., 'Quincy North'). Empty for all sites.
        metric: Specific metric to focus on (e.g., 'latency', 'errors', 'temperature').
    """
    key = "quincy_north" if "quincy" in site.lower() else "default"
    result = MOCK_TELEMETRY[key]
    return json.dumps({"status": "success", "telemetry": result}, indent=2)


def dispatch_work_order(
    title: str,
    priority: str,
    site: str,
    description: str,
    assignee: str = "Field Tech On-Site",
) -> str:
    """Create and dispatch a new work order to field operations.

    Args:
        title: Brief title for the work order.
        priority: Priority level - P1 (Critical), P2 (High), P3 (Normal).
        site: Site location for the work.
        description: Detailed description of the work to be done.
        assignee: Who to assign (default: Field Tech On-Site).
    """
    wo_id = (
        f"WO-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{hash(title) % 1000:03d}"
    )
    result = {
        "status": "success",
        "work_order": {
            "id": wo_id,
            "title": title,
            "priority": priority,
            "site": site,
            "description": description,
            "assignee": assignee,
            "status": "Dispatched",
            "created": datetime.now(timezone.utc).isoformat(),
        },
        "message": f"Work order {wo_id} created and dispatched to {assignee}.",
    }
    return json.dumps(result, indent=2)


def request_approval(
    action: str, description: str, severity: str = "critical", context: str = ""
) -> str:
    """Request human approval before executing a destructive or high-impact action.

    IMPORTANT: You MUST call this tool before dispatching work orders or executing
    any action that could cause service interruption, data loss, or safety risk.
    The action will NOT proceed until a human operator approves it.

    Args:
        action: Short title of the proposed action (e.g. "Dispatch emergency repair crew").
        description: Detailed explanation of what will happen and potential impact.
        severity: Risk level - "critical" (service impact) or "warning" (operational risk).
        context: Additional context like affected subscribers, estimated downtime, etc.
    """
    import uuid

    instance_id = f"inv-{uuid.uuid4().hex[:12]}"

    # NOTE: This tool must NOT schedule a DTS orchestration itself.
    # The durable approval gate IS the `investigation_with_approval`
    # orchestration, and that orchestration runs this same agent in Step 1 —
    # so if request_approval scheduled a new orchestration, every orchestration
    # would spawn another via its own agent step, creating an unbounded
    # feedback loop. Orchestration scheduling is owned by the trigger boundary
    # (the demo backend / entry point), not by this tool. Here we only surface
    # the approval request (return value + Teams card).

    # Proactively push the approval adaptive card to Jeff in Teams (best effort).
    # During the heartbeat routine this is what pokes the operator with an
    # Approve/Reject card; clicking Approve resolves the same DTS gate.
    teams_notified = False
    try:
        from teams_bot import send_approval_card_sync

        teams_notified = send_approval_card_sync(
            action=action,
            description=description,
            severity=severity,
            approval_id=instance_id,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Teams approval card not sent: %s", e)

    result = {
        "status": "approval_pending",
        "approval_id": instance_id,
        "action": action,
        "description": description,
        "severity": severity,
        "teams_notified": teams_notified,
        "message": (
            f"\u26a0\ufe0f APPROVAL REQUIRED\n\n"
            f"Action: {action}\n"
            f"Impact: {description}\n"
            f"Severity: {severity}\n"
            f"Approval ID: {instance_id}\n\n"
            f"This action is paused and waiting for human approval. "
            f"The operator must approve before execution can proceed."
        ),
    }
    return json.dumps(result, indent=2)


def save_investigation(filename: str, content: str, incident_id: str = "") -> str:
    """Save investigation notes or findings to persistent storage for audit trail.

    Args:
        filename: Name for the file (e.g., 'rack-b14-analysis.md').
        content: Content to save (investigation notes, findings, etc.).
        incident_id: Related incident ID for cross-reference.
    """
    try:
        INVESTIGATIONS_DIR.mkdir(parents=True, exist_ok=True)
        filepath = INVESTIGATIONS_DIR / filename
        header = f"# Investigation: {filename}\n"
        header += f"# Saved: {datetime.now(timezone.utc).isoformat()}\n"
        if incident_id:
            header += f"# Related Incident: {incident_id}\n"
        header += "---\n\n"
        filepath.write_text(header + content)
        return json.dumps(
            {
                "status": "success",
                "message": f"Investigation saved to {filepath}",
                "path": str(filepath),
                "size_bytes": filepath.stat().st_size,
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps(
            {"status": "error", "message": f"Failed to save: {e}"}, indent=2
        )


def get_active_incidents(site: str = "", severity: str = "", status: str = "") -> str:
    """Get list of active incidents and their current status.

    Args:
        site: Filter by site name.
        severity: Filter by severity (P1, P2, P3, P4).
        status: Filter by status (investigating, monitoring, resolved, escalated, waiting-for-vendor).
    """
    results = MOCK_INCIDENTS
    if site:
        results = [inc for inc in results if site.lower() in inc["site"].lower()]
    if severity:
        results = [
            inc for inc in results if severity.lower() in inc["severity"].lower()
        ]
    if status:
        results = [inc for inc in results if status.lower() in inc["status"].lower()]

    # Return summary table format for broad queries (no filters or many results)
    if len(results) > 5:
        table_rows = []
        for inc in results:
            table_rows.append(
                {
                    "incident_id": inc["id"],
                    "site": inc["site"],
                    "status": inc["status"],
                    "type": inc.get("type", "unknown"),
                    "priority": inc["severity"],
                    "title": inc["title"],
                    "last_updated": inc["last_update"],
                }
            )
        return json.dumps(
            {
                "status": "success",
                "count": len(results),
                "summary": "Showing summary table. Ask about a specific incident ID for full details.",
                "incidents": table_rows,
            },
            indent=2,
        )
    else:
        # Return full detail for narrow queries
        return json.dumps(
            {"status": "success", "count": len(results), "incidents": results}, indent=2
        )


def escalate_incident(
    incident_id: str, reason: str, escalate_to: str = "Engineering Lead"
) -> str:
    """Escalate an incident to a higher tier of support.

    Args:
        incident_id: The incident ID to escalate.
        reason: Why escalation is needed.
        escalate_to: Role/team to escalate to (default: Engineering Lead).
    """
    result = {
        "status": "success",
        "message": f"Incident {incident_id} escalated to {escalate_to}.",
        "escalation": {
            "incident_id": incident_id,
            "escalated_to": escalate_to,
            "reason": reason,
            "escalated_at": datetime.now(timezone.utc).isoformat(),
            "new_severity": "P1",
            "sla_response_minutes": 15,
        },
    }
    return json.dumps(result, indent=2)


# ── System Prompt ─────────────────────────────────────────────────────────────

def _load_system_prompt() -> str:
    configured = os.getenv("FIBEY_SYSTEM_PROMPT_FILE", "").strip()
    default_path = (
        pathlib.Path(__file__).resolve().parent
        / ".agent_configs"
        / "baseline"
        / "instructions.md"
    )
    prompt_path = pathlib.Path(configured) if configured else default_path
    if not prompt_path.is_absolute():
        prompt_path = (pathlib.Path(__file__).resolve().parent / prompt_path).resolve()
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"Fibey prompt file not found: {prompt_path}. "
            "Set FIBEY_SYSTEM_PROMPT_FILE to a valid path."
        )
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"Fibey prompt file is empty: {prompt_path}")
    return prompt


SYSTEM_PROMPT = _load_system_prompt()


# ── MAF Agent Setup ───────────────────────────────────────────────────────────

credential = DefaultAzureCredential()

_agent = None
_agent_lock = asyncio.Lock()


def _create_agent():
    """Create and return the MAF agent with local function tools."""
    chat_client = FoundryChatClient(
        project_endpoint=PROJECT_ENDPOINT,
        model=MODEL_DEPLOYMENT_NAME,
        credential=credential,
        allow_preview=True,
    )

    tools = [
        FunctionTool(func=check_network_telemetry, name="check_network_telemetry"),
        FunctionTool(func=request_approval, name="request_approval"),
        FunctionTool(func=dispatch_work_order, name="dispatch_work_order"),
        FunctionTool(func=save_investigation, name="save_investigation"),
        FunctionTool(func=get_active_incidents, name="get_active_incidents"),
        FunctionTool(func=escalate_incident, name="escalate_incident"),
    ]

    toolbox_tool = build_toolbox_tool(credential)
    if toolbox_tool is not None:
        tools.append(toolbox_tool)

    agent = chat_client.as_agent(
        name=AGENT_NAME,
        instructions=SYSTEM_PROMPT,
        tools=tools,
    )

    logger.info(
        "[%s] starting up (model=%s, endpoint=%s)",
        AGENT_NAME,
        MODEL_DEPLOYMENT_NAME,
        PROJECT_ENDPOINT,
    )
    return agent


async def _get_agent():
    global _agent
    if _agent is not None:
        return _agent
    async with _agent_lock:
        if _agent is not None:
            return _agent
        _agent = _create_agent()
        return _agent


# ── Server ────────────────────────────────────────────────────────────────────

responses = ResponsesAgentServerHost(
    options=ResponsesServerOptions(default_fetch_history_count=20),
)


def _get_input_text(request: CreateResponse) -> str | None:
    """Extract plain text from a CreateResponse input."""
    inp = request.input
    if isinstance(inp, str):
        return inp
    items = get_input_expanded(request)
    for item in items:
        content = getattr(item, "content", None)
        if content is None:
            continue
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                text = getattr(part, "text", None)
                if text:
                    return text
    return None


@responses.response_handler
async def handle_response(
    request: CreateResponse,
    context: ResponseContext,
    cancellation_signal: asyncio.Event,
):
    stream = ResponseEventStream(
        response_id=context.response_id,
        model=getattr(request, "model", None),
    )

    yield stream.emit_created()
    yield stream.emit_in_progress()

    user_input = _get_input_text(request) or ""
    if not user_input:
        message_item = stream.add_output_item_message()
        yield message_item.emit_added()
        for event in message_item.text_content("No input provided."):
            yield event
        yield message_item.emit_done()
        yield stream.emit_completed()
        return

    try:
        agent = await _get_agent()
        result = await asyncio.wait_for(
            agent.run(messages=user_input, stream=False),
            timeout=120.0,
        )
        # Extract text from MAF AgentResponse
        assistant_reply = (
            str(result.message) if hasattr(result, "message") else str(result)
        )
        if not assistant_reply:
            assistant_reply = "(Agent completed without text response)"
    except asyncio.TimeoutError:
        assistant_reply = "Request timed out. Please retry with a simpler query."
    except asyncio.CancelledError:
        assistant_reply = "Request was cancelled. Please retry."
    except Exception as e:
        logger.error("Failed to process request: %s", e, exc_info=True)
        assistant_reply = f"I encountered an error processing your request: {e}"

    message_item = stream.add_output_item_message()
    yield message_item.emit_added()

    for event in message_item.text_content(assistant_reply):
        yield event
    yield message_item.emit_done()

    yield stream.emit_completed()


# --- Durable Task Extension ---
# Eagerly create the MAF agent and register it as a durable agent
# (chat history + tool calls auto-persisted to DTS by the Durable
# Extension for Microsoft Agent Framework).
from durable_orchestration import durable_manager

_agent = _create_agent()
durable_manager.register_agent(_agent)

from durable_startup import bootstrap

bootstrap()

# --- Teams Activity Protocol passthrough ---
# Mount POST /api/messages on the same agentserver port so the Foundry
# activity-protocol endpoint can forward raw Bot Framework activities here.
import teams_bot

teams_bot.configure(agent_getter=_get_agent, durable_manager=durable_manager)
responses.add_route("/api/messages", teams_bot.handle_messages, methods=["POST"])

responses.run()

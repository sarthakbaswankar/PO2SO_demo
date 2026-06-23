"""
config.py
=========
All credentials and environment settings for the PO automation pipeline.

HOW TO USE
----------
Set values either via environment variables (recommended for production)
or by editing the defaults below for local development.

Environment variables take precedence over defaults.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


# ─────────────────────────────────────────────────────────────────────────────
# OCI Object Storage
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class StorageConfig:
    # !! CHANGE: your OCI tenancy namespace (visible in Object Storage console)
    namespace: str = os.getenv("OCI_NAMESPACE", "bmarpjct5zvz")

    # !! CHANGE: bucket name where PO PDFs are stored
    bucket_name: str = os.getenv("OCI_BUCKET_NAME", "PO2SO")

    # Folder paths inside the bucket
    input_folder: str = os.getenv("OCI_INPUT_FOLDER",  "POPDFS/")
    processed_folder: str = os.getenv("OCI_PROCESSED_FOLDER", "processed/")
    error_folder: str = os.getenv("OCI_ERROR_FOLDER",  "error/")

    # !! CHANGE: OCI region (e.g. "ap-mumbai-1", "us-ashburn-1")
    region: str = os.getenv("OCI_REGION", "us-ashburn-1")

    # !! CHANGE: path to your OCI config file (~/.oci/config by default)
    oci_config_path: str = os.getenv("OCI_CONFIG_PATH", "~/.oci/config")

    # !! CHANGE: profile name inside your OCI config file
    oci_profile: str = os.getenv("OCI_PROFILE", "DEFAULT")


# ─────────────────────────────────────────────────────────────────────────────
# OCI Generative AI
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class GenAIConfig:
    # !! CHANGE: your OCI compartment OCID
    compartment_id: str = os.getenv(
        "OCI_COMPARTMENT_ID",
        "ocid1.tenancy.oc1..aaaaaaaacugfkiaqlmvwu7xyh3lknlokf3baz3ymgfdnaxkwuaansrdcycca"
    )

    # !! CHANGE: GenAI service endpoint for your region.
    # IMPORTANT: Gemini 2.5 Pro is only hosted in specific OCI regions
    # (e.g. US East Ashburn via the Google interconnect). If the Gemini call
    # fails with a "model not found / not available in region" error, set this
    # to a region where google.gemini-2.5-pro is offered. Check OCI's
    # "Pretrained Foundational Models by Region" page for the current list.
    service_endpoint: str = os.getenv(
        "OCI_GENAI_ENDPOINT",
        "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com"
    )

    # Model to use. Switched to Google Gemini 2.5 Pro (via OCI Generative AI).
    # OCI model name for Gemini 2.5 Pro is "google.gemini-2.5-pro".
    model_id: str = os.getenv(
        "OCI_GENAI_MODEL_ID",
        "google.gemini-2.5-pro"
    )

    max_tokens: int = int(os.getenv("OCI_GENAI_MAX_TOKENS", "65336"))
    temperature: float = float(os.getenv("OCI_GENAI_TEMPERATURE", "0.5"))


# ─────────────────────────────────────────────────────────────────────────────
# Oracle BIP (BI Publisher)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class BIPConfig:
    # !! CHANGE: Oracle Fusion base URL e.g. "https://your-instance.oraclecloud.com"
    oracle_base_url: str = os.getenv(
        "ORACLE_BASE_URL",
        "https://fa-epxp-test-saasfaprod1.fa.ocs.oraclecloud.com"
    )

    # !! CHANGE: Oracle Fusion username
    oracle_username: str = os.getenv("ORACLE_USERNAME", "suraj.yadav@pinelabs.com")

    # !! CHANGE: Oracle Fusion password
    oracle_password: str = os.getenv("ORACLE_PASSWORD", "India@123@")

    # !! CHANGE: Full path to the BI report in BIP catalog
    # e.g. "/Custom/Reports/Sales/Customer_Data.xdo"
    report_path: str = os.getenv(
        "BIP_REPORT_PATH",
        "/Custom/Sarthak_PO/PO_report.xdo"
    )

    # Customer-item cross-reference report (maps the customer's item number on
    # the PO to the internal Fusion Item Number before the Sales Order is created).
    xref_report_path: str = os.getenv(
        "BIP_XREF_REPORT_PATH",
        "/Custom/Sarthak_PO/Trading Partner/Trading Partner Item Details Report.xdo"
    )
    # BIP parameter names for the cross-reference report (matching :p_party_name /
    # :p_party_number in the report's SQL). Change here if your data model uses
    # different parameter tokens.
    xref_party_name_param: str = os.getenv("BIP_XREF_PARTY_NAME_PARAM", "P_PARTY_NAME")
    xref_party_number_param: str = os.getenv("BIP_XREF_PARTY_NUMBER_PARAM", "P_PARTY_NUMBER")
    # When False (default), the cross-reference report is called WITHOUT any
    # parameters — it returns every trading-partner mapping and we match the
    # customer item locally. This avoids any parameter-name mismatch silently
    # filtering the report to zero rows. Set True to filter by party at the report.
    xref_filter_by_party: bool = os.getenv("BIP_XREF_FILTER_BY_PARTY", "false").lower() == "true"

    # Customer ship-to ADDRESS report. The report returns every ship-to address
    # on file for the customer (one parameter: Customer Name), each row carrying
    # the ShipToPartyId / ShipToPartySiteId / ShipToSiteUseId needed for the SO.
    # The user confirmed this is the SAME report as the customer reference, so it
    # defaults to report_path; override only if you split it into its own .xdo.
    address_report_path: str = os.getenv("BIP_ADDRESS_REPORT_PATH", "") or os.getenv(
        "BIP_REPORT_PATH", "/Custom/Sarthak_PO/PO_report.xdo"
    )

    # Order History report (one parameter: Customer Name). Returns full order
    # history with a per-ship-to-site shipment frequency measure. Used ONLY as
    # a tie-breaker when the address matcher lands on an AMBIGUOUS match
    # between near-identical sites — never called on a confident match.
    order_history_report_path: str = os.getenv(
        "BIP_ORDER_HISTORY_REPORT_PATH",
        "/Custom/Sarthak_PO/Order History/order_history_report.xdo",
    )
    # How far back (from today) a shipment still counts as "recent" when
    # breaking a tie between near-identical sites — whichever ambiguous site
    # has more recent shipments wins.
    order_history_recent_days: int = int(os.getenv("BIP_ORDER_HISTORY_RECENT_DAYS", "180"))

    request_timeout: int = int(os.getenv("BIP_TIMEOUT", "3000"))


# ─────────────────────────────────────────────────────────────────────────────
# Oracle Sales Order REST API
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SalesOrderConfig:
    # !! CHANGE: same Oracle base URL as BIP
    oracle_base_url: str = os.getenv(
        "ORACLE_BASE_URL",
        "https://fa-epxp-test-saasfaprod1.fa.ocs.oraclecloud.com"
    )

    # !! CHANGE: same credentials as BIP
    oracle_username: str = os.getenv("ORACLE_USERNAME", "suraj.yadav@pinelabs.com")
    oracle_password: str = os.getenv("ORACLE_PASSWORD", "India@123@")

    # REST API endpoint path (standard Oracle Fusion path)
    api_path: str = "/fscmRestApi/resources/11.13.18.05/salesOrdersForOrderHub"

    # Source transaction system — must match what's set up in Oracle
    # !! CHANGE: your configured source system code
    source_transaction_system: str = os.getenv("SO_SOURCE_SYSTEM", "OPS")

    transaction_type_code: str = os.getenv("SO_TRANSACTION_TYPE_CODE", "STD_SALES_ORDER")

    request_timeout: int = int(os.getenv("SO_TIMEOUT", "300"))

    # Submit the order immediately (true) or save as draft (false)
    submitted_flag: bool = os.getenv("SO_SUBMITTED_FLAG", "false").lower() == "true"

    # ── Attachment mode ─────────────────────────────────────────────────────
    # "url"  → upload the PDF to processed/, create a PAR, attach the URL.
    # "file" → base64-encode the PDF and attach as a file (legacy behavior).
    # !! CHANGE: pick "url" or "file"
    attachment_mode: str = os.getenv("SO_ATTACHMENT_MODE", "url").lower()

    # PAR (Pre-Authenticated Request) lifetime, in seconds.
    # Default is ~7 years to cover typical financial audit retention.
    # Anyone with the URL during this window can download the PDF without OCI login.
    # !! CHANGE if your compliance window is different.
    par_ttl_seconds: int = int(os.getenv("SO_PAR_TTL_SECONDS", str(60 * 60 * 24 * 365 * 7)))


# ─────────────────────────────────────────────────────────────────────────────
# Oracle Integration Cloud (OIC) — "Check inbox & create orders" trigger
# ─────────────────────────────────────────────────────────────────────────────
# Straightforward trigger, exactly like calling the integration's REST endpoint
# in Postman: one HTTP request to a full URL, with Basic Auth. Hitting this URL
# runs the OIC integration that reads the email inbox and drops PO PDFs into the
# POPDFS/ bucket folder, which this pipeline then processes.
@dataclass
class OICConfig:
    # !! CHANGE: the full trigger URL of your OIC integration (the same URL you
    # call in Postman). Copy it verbatim from the integration's REST endpoint.
    trigger_url: str = os.getenv(
        "OIC_TRIGGER_URL",
        "https://acse-dev-bmarpjct5zvz-ia.integration.us-ashburn-1.ocp.oraclecloud.com"
        "/ic/api/integration/v2/flows/rest/project/PO2SO/PO2SO/1.0/v1/po2so",
    )

    # HTTP method for the trigger (GET, as shown in Postman; change if your
    # integration's trigger uses POST).
    method: str = os.getenv("OIC_METHOD", "GET").upper()

    # ── HTTP Basic Auth (same as the Basic Auth tab in Postman) ─────────────
    # !! CHANGE: OIC service user + password.
    oic_username: str = os.getenv("OIC_USERNAME", "sbaswankar@acsesolutions.com")
    oic_password: str = os.getenv("OIC_PASSWORD", "SuperMan@1234567")

    # ── Error-notification integration ──────────────────────────────────────
    # Triggered AFTER a file is moved to the Object Storage error/ folder, to
    # send an error notification. Same OIC host + same Basic Auth as the inbox
    # trigger above; only the flow path differs (…/PO2SO_ERR/1.0/v1/po2so).
    error_trigger_url: str = os.getenv(
        "OIC_ERROR_TRIGGER_URL",
        "https://acse-dev-bmarpjct5zvz-ia.integration.us-ashburn-1.ocp.oraclecloud.com"
        "/ic/api/integration/v2/flows/rest/project/PO2SO/PO2SO_ERR/1.0/v1/po2so",
    )
    error_method: str = os.getenv("OIC_ERROR_METHOD", "GET").upper()
    # Master on/off for the error notification (kept separate from the inbox one).
    error_notification_enabled: bool = (
        os.getenv("OIC_ERROR_NOTIFICATION_ENABLED", "true").lower() == "true"
    )

    # ── New-ship-to-address notification integration ────────────────────────
    # Triggered when the ship-to address matcher gives up entirely (no address
    # on file is even close to the PDF's ship-to) — i.e. the PO likely carries a
    # brand-new ship-to address that needs approval before a Sales Order can be
    # created for it. NOT triggered on "needs_review" (an ambiguous but similar
    # address already exists — that's a human pick, not a new address).
    shipto_notify_trigger_url: str = os.getenv(
        "OIC_SHIPTO_NOTIFY_TRIGGER_URL",
        "https://acse-dev-bmarpjct5zvz-ia.integration.us-ashburn-1.ocp.oraclecloud.com"
        "/ic/api/integration/v2/flows/rest/project/PO2SO/PO2SO_SHIPTO_APPROVAL_NOTIFY"
        "/1.0/shiptoapproval",
    )
    shipto_notify_method: str = os.getenv("OIC_SHIPTO_NOTIFY_METHOD", "POST").upper()
    shipto_notify_enabled: bool = (
        os.getenv("OIC_SHIPTO_NOTIFY_ENABLED", "true").lower() == "true"
    )
    # Fixed fields sent on every new-ship-to-address notification (not derived
    # from the PO — only customerNumber/customerName/shipToAddress are).
    shipto_requestor_email: str = os.getenv(
        "OIC_SHIPTO_REQUESTOR_EMAIL", "requestor@company.com"
    )
    shipto_approval_link: str = os.getenv(
        "OIC_SHIPTO_APPROVAL_LINK", "https://your-streamlit-url/approve"
    )

    request_timeout: int = int(os.getenv("OIC_TIMEOUT", "500"))

    # After a successful trigger, the integration drops PO PDFs into the bucket
    # asynchronously. These control how long to wait for files to appear before
    # running the pipeline, and how often to re-check the bucket.
    wait_for_files_seconds: int = int(os.getenv("OIC_WAIT_FOR_FILES_SECONDS", "60"))
    poll_interval_seconds: int = int(os.getenv("OIC_POLL_INTERVAL_SECONDS", "5"))

    # Master on/off switch so the button can be disabled until OIC is configured.
    enabled: bool = os.getenv("OIC_ENABLED", "true").lower() == "true"


# ─────────────────────────────────────────────────────────────────────────────
# Processing / concurrency
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ProcessingConfig:
    # How many PDFs to process at the same time. The pipeline is I/O-bound
    # (GenAI, BIP, and Oracle API calls), so a thread pool gives real speedup.
    # Default 6; keep modest to avoid hitting Oracle / GenAI rate limits.
    # !! CHANGE via env PO2SO_MAX_WORKERS if you hit rate limits.
    max_workers: int = int(os.getenv("PO2SO_MAX_WORKERS", "6"))


# ─────────────────────────────────────────────────────────────────────────────
# UOM conversions (line-level unit-of-measure transforms)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class UOMConfig:
    # YAML file holding the conversion rules. Read AND written by the
    # "UOM Conversions" UI page. Defaults to data/uom_conversions.yaml next to
    # the code (uom_converter.py resolves the same default independently).
    conversions_path: str = os.getenv(
        "PO2SO_UOM_CONVERSIONS_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "data", "uom_conversions.yaml"),
    )
    # Master on/off so conversions can be disabled without deleting the file.
    enabled: bool = os.getenv("PO2SO_UOM_ENABLED", "true").lower() == "true"


# ─────────────────────────────────────────────────────────────────────────────
# Root settings object (single import point)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Settings:
    storage: StorageConfig = field(default_factory=StorageConfig)
    genai: GenAIConfig = field(default_factory=GenAIConfig)
    bip: BIPConfig = field(default_factory=BIPConfig)
    sales_order: SalesOrderConfig = field(default_factory=SalesOrderConfig)
    oic: OICConfig = field(default_factory=OICConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    uom: UOMConfig = field(default_factory=UOMConfig)


# Singleton — import this everywhere
settings = Settings()
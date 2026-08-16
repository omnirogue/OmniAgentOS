/**
 * Dev fixtures for the capability/permission system. Enable with
 * NEXT_PUBLIC_USE_CAPABILITIES_FIXTURES=true (mirrors the collab feature's flag) so
 * these pages render standalone against the real backend contract
 * (omniagentos/api/routes/access.py + api/routes/collab.py's POST /agents) without a
 * server running.
 *
 * FIXTURE_CATALOG below mirrors configs/connectors.yaml as shipped (63
 * connectors, 131 capabilities, 10 groups). `callable_now` matches
 * the backend's current reviewed set (stripe_*.read, piedpiper_acmeuni.read/note_write/
 * tag_write/opportunity_move, meta_acmeuni.read, meta_initech.read, fanbasis.read,
 * google_sheets.*, google_docs.*, google_drive_files.*); everything else is
 * declared but not yet callable, matching the shipped registry. `always_human` is true exactly for `consequential`
 * capabilities, mirroring broker.HARD_HUMAN_CLASSES.
 */
import { pushFixtureAgent } from "@/features/collab/fixtures";
import type {
  AccessLogEntry,
  AgentAccess,
  CapabilitiesCatalog,
  CreateAgentRequest,
  CreateAgentResponse,
  ServerInfo,
} from "./types";
import { computeGrantSummary } from "./riskModel";

/** Enable locally with NEXT_PUBLIC_USE_CAPABILITIES_FIXTURES=true. */
export const USE_FIXTURES = process.env.NEXT_PUBLIC_USE_CAPABILITIES_FIXTURES === "true";

export const FIXTURE_CATALOG: CapabilitiesCatalog = {
  "groups": {
    "payments": {
      "label": "Payments & banking",
      "danger": true
    },
    "ads": {
      "label": "Advertising",
      "danger": true
    },
    "crm": {
      "label": "CRM & customers",
      "danger": false
    },
    "comms": {
      "label": "Communications",
      "danger": false
    },
    "analytics": {
      "label": "Analytics & experimentation",
      "danger": false
    },
    "ai": {
      "label": "AI & media generation",
      "danger": false
    },
    "infra": {
      "label": "Infrastructure",
      "danger": true
    },
    "monitoring": {
      "label": "Monitoring",
      "danger": false
    },
    "knowledge": {
      "label": "Knowledge base",
      "danger": false
    },
    "google": {
      "label": "Google Workspace",
      "danger": false
    }
  },
  "connectors": [
    {
      "id": "knowledge",
      "label": "OmniAgentOS knowledge base",
      "group": "knowledge",
      "env_names": [],
      "capabilities": [
        {
          "id": "knowledge.read",
          "label": "Read active knowledge facts and graph",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "knowledge.write",
          "label": "Ingest web and chat knowledge into quarantine",
          "action_class": "internal_reversible",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "stripe_acmeuni",
      "label": "Stripe \u2014 AcmeUni",
      "group": "payments",
      "env_names": [
        "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY",
        "ACMEUNI_STRIPE_SECONDARY_SECRET_KEY"
      ],
      "capabilities": [
        {
          "id": "stripe_acmeuni.read",
          "label": "Read charges, customers, subscriptions, payouts",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "stripe_acmeuni.refund",
          "label": "Issue a refund",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        },
        {
          "id": "stripe_acmeuni.charge",
          "label": "Create a charge or subscription",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "stripe_initech",
      "label": "Stripe \u2014 Initech",
      "group": "payments",
      "env_names": [
        "INITECH_STRIPE_PRIMARY_SECRET_KEY"
      ],
      "capabilities": [
        {
          "id": "stripe_initech.read",
          "label": "Read charges, customers, subscriptions, payouts",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "stripe_initech.refund",
          "label": "Issue a refund",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        },
        {
          "id": "stripe_initech.charge",
          "label": "Create a charge or subscription",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "stripe_primary",
      "label": "Stripe \u2014 global primary account",
      "group": "payments",
      "env_names": [
        "STRIPE_PRIMARY_SECRET_KEY"
      ],
      "capabilities": [
        {
          "id": "stripe_primary.read",
          "label": "Read global primary-account charges",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        }
      ]
    },
    {
      "id": "stripe_secondary",
      "label": "Stripe \u2014 global secondary account",
      "group": "payments",
      "env_names": [
        "STRIPE_SECONDARY_SECRET_KEY"
      ],
      "capabilities": [
        {
          "id": "stripe_secondary.read",
          "label": "Read global secondary-account charges",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        }
      ]
    },
    {
      "id": "slash_bank",
      "label": "Slash \u2014 business banking (charge card + cash)",
      "group": "payments",
      "env_names": [
        "SLASH_API_KEY",
        "SLASH_API_KEY_ACMEUNI",
        "SLASH_API_KEY_INITECH"
      ],
      "capabilities": [
        {
          "id": "slash_bank.read",
          "label": "Read accounts, balances, transactions",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "slash_bank.read_acmeuni",
          "label": "Read AcmeUni Slash accounts, balances, transactions",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "slash_bank.read_initech",
          "label": "Read Initech Slash accounts, balances, transactions",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "slash_bank.transfer",
          "label": "Move money out of Slash (transfer / payment / recipient)",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "paypal",
      "label": "PayPal",
      "group": "payments",
      "env_names": [
        "PAYPAL_READONLY_CLIENT_ID",
        "PAYPAL_READONLY_CLIENT_SECRET",
        "PAYPAL_WRITE_CLIENT_ID",
        "PAYPAL_WRITE_CLIENT_SECRET"
      ],
      "capabilities": [
        {
          "id": "paypal.read",
          "label": "Read transactions and balances (uses the read-only client)",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "paypal.refund",
          "label": "Refund a transaction (uses the write client)",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        },
        {
          "id": "paypal.payout",
          "label": "Send a payout",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "paypal_claude",
      "label": "PayPal \u2014 Agent Pro Academy",
      "group": "payments",
      "env_names": [
        "PAYPAL_CLAUDE_CLIENT_ID",
        "PAYPAL_CLAUDE_CLIENT_SECRET",
        "PAYPAL_CLAUDE_ENV"
      ],
      "capabilities": [
        {
          "id": "paypal_claude.read",
          "label": "Read transactions and balances",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "paypal_claude.refund",
          "label": "Refund a transaction",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        },
        {
          "id": "paypal_claude.payout",
          "label": "Send a payout",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "nmi_fortpoint",
      "label": "NMI \u2014 Fortpoint gateway",
      "group": "payments",
      "env_names": [
        "FORTPOINT_NMI_SECURITY_KEY",
        "FORTPOINT_NMI_USERNAME",
        "FORTPOINT_NMI_API_BASE"
      ],
      "capabilities": [
        {
          "id": "nmi_fortpoint.read",
          "label": "Query transactions",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "nmi_fortpoint.refund",
          "label": "Refund or void a transaction",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "nmi_glacier",
      "label": "NMI \u2014 Glacier gateway",
      "group": "payments",
      "env_names": [
        "GLACIER_NMI_SECURITY_KEY",
        "GLACIER_NMI_USERNAME",
        "GLACIER_NMI_API_BASE"
      ],
      "capabilities": [
        {
          "id": "nmi_glacier.read",
          "label": "Query transactions",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "nmi_glacier.refund",
          "label": "Refund or void a transaction",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "chargeblast",
      "label": "ChargeBlast (chargeback alerts)",
      "group": "payments",
      "env_names": [
        "ACMEUNI_CHARGEBLAST_PRIMARY_API_KEY",
        "ACMEUNI_CHARGEBLAST_SECONDARY_API_KEY",
        "INITECH_CHARGEBLAST_API_KEY"
      ],
      "capabilities": [
        {
          "id": "chargeblast.read",
          "label": "Read chargeback and dispute alerts",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "chargeblast.refund",
          "label": "Pre-emptively refund an alerted transaction",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "vandelay",
      "label": "Vandelay",
      "group": "payments",
      "env_names": [
        "VANDELAY_LOGIN_ID",
        "VANDELAY_PASSWORD",
        "VANDELAY_BASE_URL"
      ],
      "capabilities": [
        {
          "id": "vandelay.read",
          "label": "Read orders, transactions, rebill schedules",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "vandelay.refund",
          "label": "Refund or cancel an order",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        },
        {
          "id": "vandelay.rebill_edit",
          "label": "Change a customer's rebill schedule",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "fanbasis",
      "label": "FanBasis",
      "group": "payments",
      "env_names": [
        "FANBASIS_API_KEY"
      ],
      "capabilities": [
        {
          "id": "fanbasis.read",
          "label": "Read payments and payouts",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        }
      ]
    },
    {
      "id": "teller",
      "label": "Teller (bank accounts)",
      "group": "payments",
      "env_names": [
        "TELLER_APPLICATION_ID",
        "TELLER_CERT_PATH",
        "TELLER_CERT_KEY_PATH",
        "TELLER_ENV",
        "TELLER_ACCESS_TOKEN",
        "TELLER_API_BASE"
      ],
      "capabilities": [
        {
          "id": "teller.read",
          "label": "Read bank balances and transactions",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        }
      ]
    },
    {
      "id": "slash",
      "label": "Slash (banking / cards)",
      "group": "payments",
      "env_names": [
        "SLASH_API_KEY"
      ],
      "capabilities": [
        {
          "id": "slash.read",
          "label": "Read account balances and card transactions",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "meta_acmeuni",
      "label": "Meta Ads \u2014 AcmeUni",
      "group": "ads",
      "env_names": [
        "ACMEUNI_META_ACCESS_TOKEN",
        "ACMEUNI_META_AD_ACCOUNT_IDS"
      ],
      "capabilities": [
        {
          "id": "meta_acmeuni.read",
          "label": "Read campaigns, ad sets, ads, insights, spend",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "meta_acmeuni.analyze",
          "label": "Produce optimisation recommendations (writes nothing to Meta)",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "meta_acmeuni.creative_draft",
          "label": "Upload a PAUSED creative or draft ad",
          "action_class": "internal_reversible",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "meta_acmeuni.budget_change",
          "label": "Change a campaign or ad set budget",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        },
        {
          "id": "meta_acmeuni.launch",
          "label": "Launch, unpause, or activate an ad",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "meta_initech",
      "label": "Meta Ads \u2014 Initech",
      "group": "ads",
      "env_names": [
        "INITECH_META_ACCESS_TOKEN",
        "INITECH_META_AD_ACCOUNT_IDS"
      ],
      "capabilities": [
        {
          "id": "meta_initech.read",
          "label": "Read campaigns, ad sets, ads, insights, spend",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "meta_initech.analyze",
          "label": "Produce optimisation recommendations (writes nothing to Meta)",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "meta_initech.creative_draft",
          "label": "Upload a PAUSED creative or draft ad",
          "action_class": "internal_reversible",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "meta_initech.budget_change",
          "label": "Change a campaign or ad set budget",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        },
        {
          "id": "meta_initech.launch",
          "label": "Launch, unpause, or activate an ad",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "meta_globex",
      "label": "Meta Ads \u2014 Globex",
      "group": "ads",
      "env_names": [
        "GLOBEX_META_ACCESS_TOKEN",
        "GLOBEX_META_AD_ACCOUNT_ID",
        "GLOBEX_META_APP_ID",
        "GLOBEX_META_APP_SECRET",
        "GLOBEX_META_BM_ID",
        "GLOBEX_META_PAGE_ID",
        "GLOBEX_META_PIXEL_ID"
      ],
      "capabilities": [
        {
          "id": "meta_globex.read",
          "label": "Read campaigns, ad sets, ads, insights, spend",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "meta_globex.analyze",
          "label": "Produce optimisation recommendations (writes nothing to Meta)",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "meta_globex.creative_draft",
          "label": "Upload a PAUSED creative or draft ad",
          "action_class": "internal_reversible",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "meta_globex.budget_change",
          "label": "Change a campaign or ad set budget",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        },
        {
          "id": "meta_globex.launch",
          "label": "Launch, unpause, or activate an ad",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "meta_agentpro",
      "label": "Meta Ads \u2014 Agent Pro Academy",
      "group": "ads",
      "env_names": [
        "ACMEUNI_AGENTPRO_META_ACCESS_TOKEN",
        "ACMEUNI_AGENTPRO_META_PIXEL_ID"
      ],
      "capabilities": [
        {
          "id": "meta_agentpro.read",
          "label": "Read campaigns, ad sets, ads, insights, spend",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "meta_agentpro.analyze",
          "label": "Produce optimisation recommendations (writes nothing to Meta)",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "meta_agentpro.creative_draft",
          "label": "Upload a PAUSED creative or draft ad",
          "action_class": "internal_reversible",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "meta_agentpro.budget_change",
          "label": "Change a campaign or ad set budget",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        },
        {
          "id": "meta_agentpro.launch",
          "label": "Launch, unpause, or activate an ad",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "metapi",
      "label": "MetAPI (competitor ad intelligence)",
      "group": "ads",
      "env_names": [
        "METAPI_API_KEY",
        "METAPI_BASE_URL"
      ],
      "capabilities": [
        {
          "id": "metapi.read",
          "label": "Read competitor ad libraries",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "piedpiper_acmeuni",
      "label": "PiedPiper \u2014 AcmeUni",
      "group": "crm",
      "env_names": [
        "PIEDPIPER_ACMEUNI_TOKEN",
        "PIEDPIPER_ACMEUNI_LOCATION_ID",
        "PIEDPIPER_COMPANY_ID"
      ],
      "capabilities": [
        {
          "id": "piedpiper_acmeuni.read",
          "label": "Read contacts, opportunities, pipelines, conversations",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "piedpiper_acmeuni.note_write",
          "label": "Add an internal note to a contact",
          "action_class": "internal_reversible",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "piedpiper_acmeuni.tag_write",
          "label": "Add or remove a contact tag",
          "action_class": "internal_reversible",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "piedpiper_acmeuni.opportunity_move",
          "label": "Move an opportunity between pipeline stages",
          "action_class": "internal_reversible",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "piedpiper_acmeuni.contact_edit",
          "label": "Edit customer-visible contact fields",
          "action_class": "external_reversible",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "piedpiper_acmeuni.message_send",
          "label": "Send an SMS or email to a contact",
          "action_class": "external_reversible",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "piedpiper_initech",
      "label": "PiedPiper \u2014 Initech",
      "group": "crm",
      "env_names": [
        "PIEDPIPER_INITECH_TOKEN",
        "PIEDPIPER_INITECH_LOCATION_ID",
        "PIEDPIPER_COMPANY_ID"
      ],
      "capabilities": [
        {
          "id": "piedpiper_initech.read",
          "label": "Read contacts, opportunities, pipelines, conversations",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "piedpiper_initech.note_write",
          "label": "Add an internal note to a contact",
          "action_class": "internal_reversible",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "piedpiper_initech.tag_write",
          "label": "Add or remove a contact tag",
          "action_class": "internal_reversible",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "piedpiper_initech.message_send",
          "label": "Send an SMS or email to a contact",
          "action_class": "external_reversible",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "piedpiper_globex",
      "label": "PiedPiper \u2014 Globex",
      "group": "crm",
      "env_names": [
        "PIEDPIPER_GLOBEX_TOKEN",
        "PIEDPIPER_GLOBEX_LOCATION_ID",
        "PIEDPIPER_COMPANY_ID"
      ],
      "capabilities": [
        {
          "id": "piedpiper_globex.read",
          "label": "Read contacts, opportunities, pipelines, conversations",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "piedpiper_globex.note_write",
          "label": "Add an internal note to a contact",
          "action_class": "internal_reversible",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "piedpiper_globex.tag_write",
          "label": "Add or remove a contact tag",
          "action_class": "internal_reversible",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "piedpiper_globex.message_send",
          "label": "Send an SMS or email to a contact",
          "action_class": "external_reversible",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "crm_internal",
      "label": "Initech CRM (fb.initech.ai)",
      "group": "crm",
      "env_names": [
        "CRM_ADMIN_SERVICE_TOKEN",
        "CRM_INTERNAL_SERVICE_TOKEN",
        "CRM_BASIC_AUTH",
        "CRM_DATABASE_URL"
      ],
      "capabilities": [
        {
          "id": "crm_internal.read",
          "label": "Read CRM reports, funnels, and live sessions",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "crm_internal.annotate",
          "label": "Write internal annotations and report notes",
          "action_class": "internal_reversible",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "crm_internal.db_read",
          "label": "Run read-only SELECT queries against the CRM database",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "crm_internal.db_write",
          "label": "Run INSERT/UPDATE/DELETE/DDL against the CRM database",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "clerk",
      "label": "Clerk (user auth)",
      "group": "crm",
      "env_names": [
        "CLERK_SECRET_KEY",
        "ACMEUNI_CLERK_SECRET_KEY",
        "INITECH_CLERK_SECRET_KEY"
      ],
      "capabilities": [
        {
          "id": "clerk.read",
          "label": "Read user accounts and sessions",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "clerk.user_edit",
          "label": "Edit user metadata or entitlements",
          "action_class": "external_reversible",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "clerk.user_delete",
          "label": "Delete or ban a user account",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "livesession",
      "label": "LiveSession",
      "group": "crm",
      "env_names": [
        "ACMEUNI_LIVESESSION_PAT"
      ],
      "capabilities": [
        {
          "id": "livesession.read",
          "label": "Read session recordings and funnel events",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "webinarjam",
      "label": "WebinarJam / EverWebinar",
      "group": "crm",
      "env_names": [
        "WEBINARJAM_API_KEY"
      ],
      "capabilities": [
        {
          "id": "webinarjam.read",
          "label": "Read webinars, registrants, and attendance analytics",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "slack",
      "label": "Slack",
      "group": "comms",
      "env_names": [
        "SLACK_WEBHOOK_URL",
        "OPS_ALERT_SLACK_WEBHOOK_URL"
      ],
      "capabilities": [
        {
          "id": "slack.post_internal",
          "label": "Post to your own ops/alert channels",
          "action_class": "internal_reversible",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "zapier",
      "label": "Zapier (MCP-connected apps)",
      "group": "comms",
      "env_names": [
        "ZAPIER_MCP_CONFIG_URL"
      ],
      "capabilities": [
        {
          "id": "zapier.read",
          "label": "Search and read data from connected apps",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "zapier.trigger",
          "label": "Trigger a configured Zap / connected-app action",
          "action_class": "external_reversible",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "google_sheets",
      "label": "Google Sheets",
      "group": "google",
      "env_names": [
        "GOOGLE_OAUTH_REFRESH_TOKEN",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET"
      ],
      "capabilities": [
        {
          "id": "google_sheets.read",
          "label": "Read spreadsheet metadata and cell values",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "google_sheets.write",
          "label": "Overwrite a range's values",
          "action_class": "external_reversible",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "google_sheets.append",
          "label": "Append rows to a range",
          "action_class": "external_reversible",
          "callable_now": true,
          "always_human": false
        }
      ]
    },
    {
      "id": "google_docs",
      "label": "Google Docs",
      "group": "google",
      "env_names": [
        "GOOGLE_OAUTH_REFRESH_TOKEN",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET"
      ],
      "capabilities": [
        {
          "id": "google_docs.read",
          "label": "Read a document's content and structure",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "google_docs.write",
          "label": "Apply edits to a document",
          "action_class": "external_reversible",
          "callable_now": true,
          "always_human": false
        }
      ]
    },
    {
      "id": "google_drive_files",
      "label": "Google Drive (files)",
      "group": "google",
      "env_names": [
        "GOOGLE_OAUTH_REFRESH_TOKEN",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET"
      ],
      "capabilities": [
        {
          "id": "google_drive_files.find",
          "label": "Search or list files",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        },
        {
          "id": "google_drive_files.get",
          "label": "Read one file's metadata or content",
          "action_class": "read_only",
          "callable_now": true,
          "always_human": false
        }
      ]
    },
    {
      "id": "vwo",
      "label": "VWO (experimentation)",
      "group": "analytics",
      "env_names": [
        "VWO_API_KEY",
        "VWO_ACCOUNT_ID"
      ],
      "capabilities": [
        {
          "id": "vwo.read",
          "label": "Read experiments and results",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "vwo.experiment_edit",
          "label": "Start, stop, or change a live experiment",
          "action_class": "external_reversible",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "kowboykit",
      "label": "KowboyKit (landers / offers / rotators)",
      "group": "analytics",
      "env_names": [
        "KOWBOYKIT_API_KEY",
        "KOWBOYKIT_API_BASE",
        "KOWBOYKIT_GROUP_ID"
      ],
      "capabilities": [
        {
          "id": "kowboykit.read",
          "label": "Read offers, landers, rotators, and reports",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "kowboykit.lander_draft",
          "label": "Write an unpublished lander draft",
          "action_class": "internal_reversible",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "kowboykit.offer_write",
          "label": "Change a live offer or rotator",
          "action_class": "external_reversible",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "checkly",
      "label": "Checkly (uptime monitoring)",
      "group": "monitoring",
      "env_names": [
        "CHECKLY_API_KEY",
        "CHECKLY_ACCOUNT_ID"
      ],
      "capabilities": [
        {
          "id": "checkly.read",
          "label": "Read uptime checks and incidents",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "anthropic",
      "label": "Anthropic",
      "group": "ai",
      "env_names": [
        "ANTHROPIC_API_KEY"
      ],
      "capabilities": [
        {
          "id": "anthropic.generate",
          "label": "Generate text",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "openai",
      "label": "OpenAI",
      "group": "ai",
      "env_names": [
        "OPENAI_API_KEY",
        "OPENAI_ORG_ID"
      ],
      "capabilities": [
        {
          "id": "openai.generate",
          "label": "Generate text",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "openrouter",
      "label": "OpenRouter",
      "group": "ai",
      "env_names": [
        "OPENROUTER_API_KEY"
      ],
      "capabilities": [
        {
          "id": "openrouter.generate",
          "label": "Generate text via any routed model",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "xai",
      "label": "xAI (Grok)",
      "group": "ai",
      "env_names": [
        "XAI_API_KEY"
      ],
      "capabilities": [
        {
          "id": "xai.generate",
          "label": "Generate text",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "google_ai",
      "label": "Google AI",
      "group": "ai",
      "env_names": [
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_AI_API_KEY"
      ],
      "capabilities": [
        {
          "id": "google_ai.generate",
          "label": "Generate text",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "moonshot",
      "label": "Moonshot AI (Kimi)",
      "group": "ai",
      "env_names": [
        "MOONSHOT_API_KEY"
      ],
      "capabilities": [
        {
          "id": "moonshot.generate",
          "label": "Generate text",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "mistral",
      "label": "Mistral",
      "group": "ai",
      "env_names": [
        "MISTRAL_API_KEY"
      ],
      "capabilities": [
        {
          "id": "mistral.generate",
          "label": "Generate text",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "together",
      "label": "Together AI",
      "group": "ai",
      "env_names": [
        "TOGETHER_API_KEY"
      ],
      "capabilities": [
        {
          "id": "together.generate",
          "label": "Generate text or run a hosted model",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "vllm",
      "label": "vLLM (self-hosted model serving)",
      "group": "ai",
      "env_names": [
        "VLLM_API_KEY"
      ],
      "capabilities": [
        {
          "id": "vllm.generate",
          "label": "Generate text via a self-hosted model",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "huggingface",
      "label": "Hugging Face",
      "group": "ai",
      "env_names": [
        "HF_TOKEN"
      ],
      "capabilities": [
        {
          "id": "huggingface.generate",
          "label": "Run inference against a hosted model",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "elevenlabs",
      "label": "ElevenLabs (voice)",
      "group": "ai",
      "env_names": [
        "ELEVENLABS_API_KEY",
        "ELEVENLABS_API_KEY_WEBINAR",
        "ELEVENLABS_VOICE_ID_OWNER",
        "ELEVENLABS_VOICE_ID_OWNERA_PRO"
      ],
      "capabilities": [
        {
          "id": "elevenlabs.generate",
          "label": "Generate speech audio",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "elevenlabs.tts",
          "label": "Text-to-speech (ElevenLabs)",
          "action_class": "sandboxed_creation",
          "callable_now": true,
          "always_human": false
        }
      ]
    },
    {
      "id": "xai_voice",
      "label": "xAI voice (probe-driven TTS)",
      "group": "ai",
      "env_names": [
        "XAI_API_KEY"
      ],
      "capabilities": [
        {
          "id": "xai_voice.tts",
          "label": "Text-to-speech (xAI, config-probed)",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "replicate",
      "label": "Replicate",
      "group": "ai",
      "env_names": [
        "REPLICATE_API_TOKEN"
      ],
      "capabilities": [
        {
          "id": "replicate.generate",
          "label": "Run an image/video model",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "higgsfield",
      "label": "Higgsfield (video)",
      "group": "ai",
      "env_names": [
        "HIGGSFIELD_API_KEY",
        "HIGGSFIELD_API_SECRET"
      ],
      "capabilities": [
        {
          "id": "higgsfield.generate",
          "label": "Generate video",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "fastlane",
      "label": "Fastlane (creative generation)",
      "group": "ai",
      "env_names": [
        "FASTLANE_API_KEY",
        "FASTLANE_API_BASE_URL"
      ],
      "capabilities": [
        {
          "id": "fastlane.generate",
          "label": "Generate ad creative",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "synclabs",
      "label": "Sync Labs (lip-sync video)",
      "group": "ai",
      "env_names": [
        "SYNC_API_KEY"
      ],
      "capabilities": [
        {
          "id": "synclabs.generate",
          "label": "Generate lip-synced video",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "piapi",
      "label": "PiAPI (media generation)",
      "group": "ai",
      "env_names": [
        "PIAPI_KEY",
        "PIAPI_BASE"
      ],
      "capabilities": [
        {
          "id": "piapi.generate",
          "label": "Generate an image or video",
          "action_class": "sandboxed_creation",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "jamendo",
      "label": "Jamendo (royalty-free music)",
      "group": "ai",
      "env_names": [
        "JAMENDO_CLIENT_ID",
        "JAMENDO_CLIENT_SECRET"
      ],
      "capabilities": [
        {
          "id": "jamendo.read",
          "label": "Search and stream royalty-free music tracks",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "pixabay",
      "label": "Pixabay (stock media)",
      "group": "ai",
      "env_names": [
        "PIXABAY_API_KEY"
      ],
      "capabilities": [
        {
          "id": "pixabay.read",
          "label": "Search and read stock images, video, and music",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "pexels",
      "label": "Pexels (stock media)",
      "group": "ai",
      "env_names": [
        "PEXELS_API_KEY"
      ],
      "capabilities": [
        {
          "id": "pexels.read",
          "label": "Search and read stock photos and video",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "postgres",
      "label": "Postgres (primary database)",
      "group": "infra",
      "env_names": [
        "DATABASE_URL"
      ],
      "capabilities": [
        {
          "id": "postgres.read",
          "label": "Run read-only SELECT queries",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "postgres.write",
          "label": "Run INSERT/UPDATE/DELETE/DDL",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "redis",
      "label": "Redis",
      "group": "infra",
      "env_names": [
        "REDIS_URL"
      ],
      "capabilities": [
        {
          "id": "redis.read",
          "label": "Read keys",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "aws",
      "label": "AWS (S3 / backups)",
      "group": "infra",
      "env_names": [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
        "S3_BUCKET"
      ],
      "capabilities": [
        {
          "id": "aws.s3_read",
          "label": "Read objects from S3",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "aws.s3_write",
          "label": "Write objects to S3",
          "action_class": "internal_reversible",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "aws.s3_delete",
          "label": "Delete objects from S3 (backups live here)",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "cloudflare",
      "label": "Cloudflare (DNS / CDN)",
      "group": "infra",
      "env_names": [
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ZONE_ID",
        "CLOUDFLARE_ACCOUNT_ID"
      ],
      "capabilities": [
        {
          "id": "cloudflare.read",
          "label": "Read DNS records and zone settings",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "cloudflare.dns_write",
          "label": "Change DNS records (can take every site offline)",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "bunny",
      "label": "Bunny CDN",
      "group": "infra",
      "env_names": [
        "BUNNY_API_KEY",
        "BUNNY_STORAGE_KEY",
        "BUNNY_STORAGE_ZONE"
      ],
      "capabilities": [
        {
          "id": "bunny.read",
          "label": "List CDN storage",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "bunny.write",
          "label": "Upload CDN assets",
          "action_class": "internal_reversible",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "bunny.purge",
          "label": "Purge the CDN cache",
          "action_class": "external_reversible",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "runpod",
      "label": "RunPod (GPU compute)",
      "group": "infra",
      "env_names": [
        "RUNPOD_API_KEY"
      ],
      "capabilities": [
        {
          "id": "runpod.read",
          "label": "Read pod status, templates, and endpoints",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "runpod.pod_create",
          "label": "Create a GPU pod (spends money by the hour)",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        },
        {
          "id": "runpod.pod_delete",
          "label": "Delete a GPU pod",
          "action_class": "consequential",
          "callable_now": false,
          "always_human": true
        }
      ]
    },
    {
      "id": "dolphin_anty",
      "label": "Dolphin{anty} (browser profiles)",
      "group": "infra",
      "env_names": [
        "DOLPHIN_TOKEN",
        "DOLPHIN_CLOUD_BASE",
        "DOLPHIN_LOCAL_BASE"
      ],
      "capabilities": [
        {
          "id": "dolphin_anty.read",
          "label": "List and read browser profiles",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "dolphin_anty.profile_control",
          "label": "Launch, stop, or modify a browser profile",
          "action_class": "external_reversible",
          "callable_now": false,
          "always_human": false
        }
      ]
    },
    {
      "id": "roblox",
      "label": "Roblox Open Cloud",
      "group": "infra",
      "env_names": [
        "ROBLOX_API_KEY",
        "ROBLOX_PLACE_ID",
        "ROBLOX_UNIVERSE_ID",
        "ROBLOX_USER_ID"
      ],
      "capabilities": [
        {
          "id": "roblox.read",
          "label": "Read universe, place, and datastore metadata",
          "action_class": "read_only",
          "callable_now": false,
          "always_human": false
        },
        {
          "id": "roblox.publish",
          "label": "Publish a place update to a live game",
          "action_class": "external_reversible",
          "callable_now": false,
          "always_human": false
        }
      ]
    }
  ]
};

/**
 * Per-agent grants for the three fixture agents in features/collab/fixtures.ts
 * (agt-1 Claude, agt-2 Codex, agt-3 Grok) -- chosen to exercise all three risk chips
 * across the roster: a broad-but-safe agent, an agent that touches a danger group and
 * needs human sign-off, and a fully read-only agent.
 *
 * Mutable module-level store (not `const`): create-agent and edit-access write
 * through this map, so a fixture session behaves statefully across navigation for as
 * long as the tab stays open, without pretending to be a real backend (it resets on
 * reload -- there is no persistence layer here, only in-memory fixtures).
 */
const FIXTURE_GRANTS: Record<string, { name: string; granted: string[] }> = {
  "agt-1": {
    name: "Claude",
    granted: [
      "piedpiper_acmeuni.read",
      "piedpiper_acmeuni.note_write",
      "piedpiper_acmeuni.tag_write",
      "piedpiper_acmeuni.opportunity_move",
      "piedpiper_acmeuni.message_send",
      "crm_internal.read",
      "crm_internal.annotate",
      "slack.post_internal",
      "vwo.read",
      "checkly.read",
      "livesession.read",
      "anthropic.generate",
      "openrouter.generate",
    ],
  },
  "agt-2": {
    name: "Codex",
    granted: [
      "postgres.read",
      "postgres.write",
      "redis.read",
      "aws.s3_read",
      "aws.s3_write",
      "aws.s3_delete",
      "cloudflare.read",
      "anthropic.generate",
      "openai.generate",
    ],
  },
  "agt-3": {
    name: "Grok",
    granted: [
      "stripe_acmeuni.read",
      "meta_acmeuni.read",
      "meta_acmeuni.analyze",
      "metapi.read",
      "vwo.read",
      "checkly.read",
      "xai.generate",
    ],
  },
};

/** Append-only in-session audit trail, seeded with the grants above. */
const FIXTURE_LOG: AccessLogEntry[] = Object.entries(FIXTURE_GRANTS).flatMap(([agentId, grant]) =>
  grant.granted.map((capId, i): AccessLogEntry => ({
    agent_id: agentId,
    capability_id: capId,
    action: "grant",
    action_class: FIXTURE_CATALOG.connectors
      .flatMap((c) => c.capabilities)
      .find((c) => c.id === capId)?.action_class ?? "read_only",
    actor: "operator",
    note: "initial fixture grant",
    ts: new Date(Date.UTC(2026, 0, 1, 0, i, 0)).toISOString(),
  })),
);

function capabilityActionClass(capId: string): AccessLogEntry["action_class"] {
  return (
    FIXTURE_CATALOG.connectors.flatMap((c) => c.capabilities).find((c) => c.id === capId)?.action_class ?? "read_only"
  );
}

function logGrantChange(agentId: string, before: string[], after: string[], note: string): void {
  const beforeSet = new Set(before);
  const afterSet = new Set(after);
  const ts = new Date().toISOString();
  for (const capId of after) {
    if (!beforeSet.has(capId)) {
      FIXTURE_LOG.push({ agent_id: agentId, capability_id: capId, action: "grant", action_class: capabilityActionClass(capId), actor: "operator", note, ts });
    }
  }
  for (const capId of before) {
    if (!afterSet.has(capId)) {
      FIXTURE_LOG.push({ agent_id: agentId, capability_id: capId, action: "revoke", action_class: capabilityActionClass(capId), actor: "operator", note, ts });
    }
  }
}

export function fixtureAgentAccess(agentId: string, agentName?: string): AgentAccess {
  const grant = FIXTURE_GRANTS[agentId];
  const granted = grant?.granted ?? [];
  return {
    agent_id: agentId,
    agent_name: agentName ?? grant?.name ?? agentId,
    granted,
    summary: computeGrantSummary(FIXTURE_CATALOG, granted),
  };
}

export function fixtureAllAgentAccess(): AgentAccess[] {
  return Object.keys(FIXTURE_GRANTS).map((agentId) => fixtureAgentAccess(agentId));
}

/**
 * Mirrors GET /api/access/servers — a small representative slice of
 * vault/servers/inventory.md (not the full fleet), enough to exercise the
 * ACTIVE / EPHEMERAL / LEGACY-STALE grouping in fixture/standalone mode.
 */
export const FIXTURE_SERVERS: ServerInfo[] = [
  {
    alias: "initech-roi-calculator",
    host: "192.0.2.11",
    user: "root",
    key: "~/.ssh/example_a.pem",
    status: "ACTIVE",
    purpose: "Initech CRM core, Meta CAPI relay, LiveSession funnel monitor, Caddy reverse proxy",
    sites: "initech-crm app + /srv/prototypes/* labs sites",
  },
  {
    alias: "agentproacademy",
    host: "192.0.2.19",
    user: "root",
    key: "~/.ssh/example_b.pem (ONLY host on this key)",
    status: "ACTIVE (live commerce)",
    purpose: "Payment webhook -> PiedPiper fulfillment, /course, /skills, /aibusiness",
    sites: "agentproacademy.com (+www, LE cert)",
  },
  {
    alias: "RunPod Qwen3.5-122B pod",
    host: "198.51.100.21 ssh port 2222, API port 8000",
    user: "root",
    key: "~/.ssh/id_ed25519",
    status: "EPHEMERAL (set up 2026-01-01)",
    purpose: "Qwen3.5-122B-A10B-Uncensored GGUF serving",
    sites: "",
  },
  {
    alias: "legacy-site-a",
    host: "203.0.113.30",
    user: "root",
    key: "~/.ssh/example_a.pem",
    status: "LEGACY-STALE (host key changed)",
    purpose: "Jordan's AI Solutions client site",
    sites: "legacy-site-a(.com)",
  },
];

/** Mirrors PUT /api/access/agents/{id}: recompute, log, and persist in-session. */
export function fixtureUpdateAgentAccess(agentId: string, granted: string[], note = ""): AgentAccess {
  const existing = FIXTURE_GRANTS[agentId];
  logGrantChange(agentId, existing?.granted ?? [], granted, note);
  FIXTURE_GRANTS[agentId] = { name: existing?.name ?? agentId, granted };
  return {
    agent_id: agentId,
    agent_name: existing?.name ?? agentId,
    granted,
    summary: computeGrantSummary(FIXTURE_CATALOG, granted),
  };
}

/** Mirrors GET /api/access/log?agent_id=... */
export function fixtureLogEntries(agentId?: string): AccessLogEntry[] {
  const entries = agentId ? FIXTURE_LOG.filter((e) => e.agent_id === agentId) : FIXTURE_LOG;
  return [...entries].sort((a, b) => b.ts.localeCompare(a.ts));
}

/**
 * Mirrors POST /api/collab/agents: assigns a fixture id, persists the grant
 * in-session, logs the initial grant, and pushes an agent row into the collab
 * fixture roster so /agents and /agents/[id] see it immediately.
 */
export function fixtureCreateAgent(payload: CreateAgentRequest): CreateAgentResponse {
  const id = `agt-fixture-${Date.now()}`;
  const now = new Date().toISOString();
  FIXTURE_GRANTS[id] = { name: payload.name, granted: payload.granted };
  logGrantChange(id, [], payload.granted, "initial grant on create");
  pushFixtureAgent({
    id,
    name: payload.name,
    lineage: payload.lineage,
    model: payload.model,
    expertise: payload.expertise,
    trust_level: payload.trust_level,
    status: "idle",
    created_at: now,
    updated_at: now,
  });
  return {
    id,
    name: payload.name,
    lineage: payload.lineage,
    model: payload.model,
    expertise: payload.expertise,
    trust_level: payload.trust_level,
    status: "idle",
    created_at: now,
    updated_at: now,
  };
}

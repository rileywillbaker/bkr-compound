// Per-provider signup instructions + paste-key field metadata, shared by the
// onboarding wizard and the Settings → Providers page. Keys pasted here are
// sent to the backend once, stored encrypted, and never returned to the
// browser again (masked preview only).

export interface ProviderField {
  field: string;
  label: string;
  placeholder: string;
}

export interface ProviderInfo {
  id: string;
  name: string;
  purpose: string;
  required: boolean;
  fields: ProviderField[];
  steps: string[];
  url: string;
}

export const PROVIDERS: ProviderInfo[] = [
  {
    id: "anthropic",
    name: "Anthropic (Claude)",
    purpose: "Powers the AI analysts, signal explanations, and chat. Required for full functionality; without it the system emits deterministic-only signals.",
    required: true,
    fields: [{ field: "api_key", label: "API key", placeholder: "sk-ant-..." }],
    url: "https://console.anthropic.com",
    steps: [
      "Go to console.anthropic.com and sign up (or sign in).",
      "Add a payment method under Settings → Billing (usage-based; a few dollars covers typical daily use).",
      "Open Settings → API Keys and click \"Create Key\".",
      "Name it (e.g. \"b-quant\"), copy the key that starts with sk-ant-, and paste it here. It is shown only once on Anthropic's site.",
    ],
  },
  {
    id: "alpaca",
    name: "Alpaca Markets",
    purpose: "Real-time and historical stock prices (free IEX feed). Required for market data.",
    required: true,
    fields: [
      { field: "api_key", label: "API Key ID", placeholder: "PK..." },
      { field: "api_secret", label: "API Secret", placeholder: "secret key" },
    ],
    url: "https://alpaca.markets",
    steps: [
      "Go to alpaca.markets and click \"Sign Up\" — choose a Paper Trading (free) account; no brokerage funding is needed.",
      "Verify your email and log in to the dashboard at app.alpaca.markets.",
      "On the home screen, find the \"API Keys\" panel on the right and click \"Generate New Keys\".",
      "Copy BOTH values: the API Key ID (starts with PK) and the Secret Key. The secret is shown only once.",
      "Paste both here. B-Quant uses Alpaca for DATA ONLY — it never places orders.",
    ],
  },
  {
    id: "finnhub",
    name: "Finnhub",
    purpose: "Company news, fundamentals, earnings calendar, insider data (free tier). Required for the news and fundamentals analysts.",
    required: true,
    fields: [{ field: "api_key", label: "API key", placeholder: "finnhub token" }],
    url: "https://finnhub.io",
    steps: [
      "Go to finnhub.io and click \"Get free API key\".",
      "Register with your email and confirm it.",
      "Your API key is displayed on the dashboard immediately after login.",
      "Copy the key and paste it here.",
    ],
  },
  {
    id: "fred",
    name: "FRED (St. Louis Fed)",
    purpose: "Macro data: VIX, interest rates, yield curve, CPI, unemployment. Required for the regime classifier and macro analyst.",
    required: true,
    fields: [{ field: "api_key", label: "API key", placeholder: "32-char hex key" }],
    url: "https://fred.stlouisfed.org",
    steps: [
      "Go to fred.stlouisfed.org and create a free account (top-right \"My Account\" → Register).",
      "Once logged in, open My Account → API Keys (or visit fred.stlouisfed.org/docs/api/api_key.html).",
      "Click \"Request API Key\", enter a short description (e.g. \"personal stock analysis\"), and accept the terms.",
      "Copy the 32-character key and paste it here.",
    ],
  },
  {
    id: "telegram",
    name: "Telegram Alerts",
    purpose: "Sends BUY/SELL alerts and daily briefs to your phone. Optional but strongly recommended.",
    required: false,
    fields: [
      { field: "bot_token", label: "Bot token", placeholder: "123456:ABC-DEF..." },
      { field: "chat_id", label: "Chat ID", placeholder: "e.g. 987654321" },
    ],
    url: "https://telegram.org",
    steps: [
      "In Telegram, search for @BotFather and send it the message /newbot.",
      "Follow the prompts: pick a display name, then a username ending in \"bot\" (e.g. my_bquant_bot).",
      "BotFather replies with an HTTP API token like 123456:ABC-DEF… — paste it here as the bot token.",
      "Now get your chat ID: search for @userinfobot in Telegram, press Start, and it replies with your numeric ID.",
      "Paste that number as the chat ID, then send YOUR new bot any message (e.g. \"hi\") so it is allowed to message you.",
      "Use \"Test connection\" — you should receive a test message within seconds.",
    ],
  },
  {
    id: "edgar",
    name: "SEC EDGAR",
    purpose: "Company filings (8-K, 10-Q/K, insider Form 4). No key needed — the SEC only requires a contact email in the request header.",
    required: false,
    fields: [{ field: "user_agent", label: "Contact info", placeholder: "B-Quant/0.1 (you@example.com)" }],
    url: "https://www.sec.gov/os/accessing-edgar-data",
    steps: [
      "No signup required. The SEC asks automated clients to identify themselves with a contact email.",
      "Enter an identifier in the form: AppName/Version (your-email@example.com).",
      "That's it — \"Test connection\" verifies EDGAR responds.",
    ],
  },
];

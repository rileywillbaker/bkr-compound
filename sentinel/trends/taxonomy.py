"""Theme taxonomy: the deterministic bridge from free text to tickers.

Every theme declares four things:

  keywords    phrases that mark a document as being ABOUT this theme. Matched
              case-insensitively on word boundaries, so "ai" never fires on
              "said" or "chain".
  etfs        thematic ETFs whose behaviour proxies investor attention. Their
              own bars are ingested (config/thematic_etfs.csv) so relative
              strength and dollar-volume trends are always available even when
              no issuer publishes free holdings.
  seeds       a starting constituent list. Deliberately NOT the final answer —
              the working universe for a theme is seeds ∪ ETF holdings ∪
              tickers extracted from theme-matched news, so a company that
              only shows up because it just won a contract still gets ranked.
  gov_queries free government/regulatory searches (Federal Register, USAspending,
              agency feeds) that indicate policy tailwind for the theme.

Adding a theme needs no code change anywhere else: scoring, ranking, the
report and the API all iterate over THEMES.

The seed lists are intentionally conservative — large, liquid, listed names.
Discovery of *smaller* beneficiaries happens through news extraction and ETF
holdings, and everything is then put through the quality gate in `ranking.py`
(no penny stocks, no micro-caps, no illiquid names) and the risk engine.
"""

from pydantic import BaseModel, Field


class Theme(BaseModel):
    """One investable theme. Pure data — no behaviour, no I/O."""

    key: str
    name: str
    description: str
    keywords: tuple[str, ...]
    etfs: tuple[str, ...] = ()
    seeds: tuple[str, ...] = ()
    gov_queries: tuple[str, ...] = ()
    # Themes whose story is mostly policy (defense, nuclear) should weight
    # government evidence higher than themes driven by product cycles (AI).
    policy_driven: bool = False
    # Retail-forum favourites need a stricter hype guard; see scoring.py.
    retail_heavy: bool = False
    sectors: tuple[str, ...] = Field(default_factory=tuple)


THEMES: tuple[Theme, ...] = (
    Theme(
        key="nuclear",
        name="Nuclear energy expansion",
        description=(
            "Reactor construction and restarts, small modular reactors, and "
            "utility power-purchase deals driven by datacenter demand."
        ),
        keywords=(
            "nuclear power", "nuclear energy", "nuclear reactor", "small modular reactor",
            "smr", "nuclear plant", "reactor restart", "nuclear licensing",
            "advanced reactor", "nuclear fuel", "enriched uranium", "haleu",
            "nuclear renaissance", "atomic energy", "fission", "nuclear capacity",
        ),
        etfs=("NLR", "URA", "URNM", "NUKZ"),
        seeds=(
            "CEG", "VST", "TLN", "NRG", "BWXT", "LEU", "SMR", "OKLO",
            "GEV", "PWR", "ETR", "D", "DUK", "EXC", "PEG",
        ),
        gov_queries=("nuclear reactor", "nuclear energy", "uranium enrichment"),
        policy_driven=True,
        sectors=("Utilities", "Energy", "Industrials"),
    ),
    Theme(
        key="uranium",
        name="Uranium supply and mining",
        description=(
            "Uranium spot pricing, mine restarts and long-term supply "
            "contracts with utilities."
        ),
        keywords=(
            "uranium", "yellowcake", "u3o8", "uranium mine", "uranium price",
            "uranium supply", "conversion capacity", "enrichment capacity",
            "uranium contract",
        ),
        etfs=("URA", "URNM", "URNJ"),
        seeds=("CCJ", "UEC", "DNN", "NXE", "UUUU", "LEU", "URG", "PDN"),
        gov_queries=("uranium", "uranium enrichment", "nuclear fuel supply"),
        policy_driven=True,
        retail_heavy=True,
        sectors=("Energy", "Basic Materials"),
    ),
    Theme(
        key="ai",
        name="Artificial intelligence buildout",
        description=(
            "Model training and inference demand, AI datacenter capex, and the "
            "compute/networking/power supply chain underneath it."
        ),
        keywords=(
            "artificial intelligence", "generative ai", "large language model",
            "ai chip", "ai accelerator", "ai datacenter", "ai data center",
            "ai infrastructure", "inference demand", "training cluster",
            "foundation model", "ai capex", "gpu demand", "neural processing",
            "ai adoption", "ai spending", "hyperscaler",
        ),
        etfs=("BOTZ", "AIQ", "IRBO", "ARKQ", "CHAT", "SMH"),
        seeds=(
            "NVDA", "AMD", "AVGO", "MSFT", "GOOGL", "META", "AMZN", "MU",
            "TSM", "SMCI", "DELL", "ANET", "MRVL", "VRT", "CRWV", "ORCL",
            "PLTR", "SNOW", "NOW", "CRM", "IBM",
        ),
        gov_queries=("artificial intelligence",),
        sectors=("Technology", "Communication Services"),
    ),
    Theme(
        key="ai_regulation",
        name="AI regulation and governance",
        description=(
            "Rules, executive actions and export controls that reshape who can "
            "sell what AI hardware and software, and to whom."
        ),
        keywords=(
            "ai regulation", "ai act", "ai safety", "ai executive order",
            "ai governance", "ai oversight", "chip export", "export control",
            "entity list", "ai rule", "algorithmic accountability",
            "ai compliance", "ai policy",
        ),
        etfs=("AIQ",),
        seeds=("NVDA", "AMD", "INTC", "AVGO", "MSFT", "GOOGL", "PLTR"),
        gov_queries=("artificial intelligence", "export administration regulations"),
        policy_driven=True,
        sectors=("Technology",),
    ),
    Theme(
        key="robotics",
        name="Robotics and automation",
        description=(
            "Industrial automation, warehouse robotics, humanoid platforms and "
            "the motion-control supply chain."
        ),
        keywords=(
            "robotics", "humanoid robot", "industrial automation", "warehouse robot",
            "autonomous mobile robot", "machine vision", "cobot",
            "factory automation", "robotic arm", "motion control",
        ),
        etfs=("BOTZ", "ROBO", "ARKQ", "IRBO"),
        seeds=("ABB", "ROK", "ISRG", "TER", "ZBRA", "EMR", "HON", "PH", "NVDA", "TSLA"),
        gov_queries=("robotics",),
        sectors=("Industrials", "Technology", "Healthcare"),
    ),
    Theme(
        key="defense",
        name="Defense and military spending",
        description=(
            "Procurement budgets, munitions replenishment, allied rearmament "
            "and new contract awards."
        ),
        keywords=(
            "defense contract", "defense spending", "defense budget", "pentagon",
            "military contract", "munitions", "missile defense", "hypersonic",
            "army awards", "navy awards", "air force awards", "rearmament",
            "nato spending", "defense procurement", "weapons system",
            "foreign military sale",
        ),
        etfs=("ITA", "PPA", "XAR", "SHLD"),
        seeds=(
            "LMT", "RTX", "NOC", "GD", "LHX", "BA", "HII", "LDOS", "TDG",
            "HWM", "AVAV", "KTOS", "CW", "BWXT", "PLTR", "RKLB",
        ),
        gov_queries=("defense procurement", "defense contract", "munitions"),
        policy_driven=True,
        sectors=("Industrials", "Technology"),
    ),
    Theme(
        key="cybersecurity",
        name="Cybersecurity",
        description=(
            "Breach-driven spending, federal security mandates and identity / "
            "cloud-security consolidation."
        ),
        keywords=(
            "cybersecurity", "cyber attack", "cyberattack", "ransomware",
            "data breach", "zero trust", "cyber threat", "security breach",
            "cyber incident", "endpoint security", "identity security",
            "cyber mandate", "critical infrastructure security",
        ),
        etfs=("HACK", "CIBR", "BUG", "IHAK"),
        seeds=(
            "PANW", "CRWD", "FTNT", "ZS", "S", "OKTA", "CYBR", "QLYS",
            "TENB", "RPD", "NET", "AKAM",
        ),
        gov_queries=("cybersecurity", "critical infrastructure protection"),
        sectors=("Technology",),
    ),
    Theme(
        key="semiconductors",
        name="Semiconductors and advanced packaging",
        description=(
            "Foundry capacity, memory pricing, advanced packaging and "
            "government-subsidised fab construction."
        ),
        keywords=(
            "semiconductor", "foundry", "wafer", "chipmaker", "chip plant",
            "fab construction", "advanced packaging", "hbm", "high bandwidth memory",
            "lithography", "euv", "chip shortage", "memory pricing",
            "node transition", "chips act", "semiconductor equipment",
        ),
        etfs=("SMH", "SOXX", "XSD", "PSI"),
        seeds=(
            "NVDA", "TSM", "AVGO", "AMD", "MU", "INTC", "AMAT", "LRCX",
            "KLAC", "ASML", "TER", "ONTO", "MRVL", "NXPI", "TXN", "ADI",
        ),
        gov_queries=("semiconductor manufacturing", "chips act"),
        sectors=("Technology",),
    ),
    Theme(
        key="clean_energy",
        name="Clean energy and storage",
        description=(
            "Solar, wind, grid-scale storage and the tax-credit regime that "
            "drives their project economics."
        ),
        keywords=(
            "solar energy", "solar panel", "wind farm", "offshore wind",
            "renewable energy", "clean energy", "battery storage",
            "energy storage", "grid storage", "tax credit", "photovoltaic",
            "green hydrogen", "decarbonization", "clean electricity",
        ),
        etfs=("ICLN", "TAN", "QCLN", "PBW", "FAN"),
        seeds=(
            "FSLR", "ENPH", "SEDG", "RUN", "NEE", "BE", "PLUG", "AMRC",
            "ARRY", "SHLS", "TSLA", "GEV",
        ),
        gov_queries=("renewable energy", "clean electricity", "energy storage"),
        policy_driven=True,
        sectors=("Energy", "Utilities", "Technology"),
    ),
    Theme(
        key="power_grid",
        name="Electrification and grid capacity",
        description=(
            "Transmission buildout, transformer and turbine shortages, and "
            "utility load growth from datacenters and electrification."
        ),
        keywords=(
            "power grid", "transmission line", "grid capacity", "load growth",
            "transformer shortage", "electrical equipment", "interconnection queue",
            "grid modernization", "electricity demand", "gas turbine",
            "utility capex", "power purchase agreement",
        ),
        etfs=("GRID", "XLU", "PAVE"),
        seeds=(
            "GEV", "ETN", "PWR", "VRT", "HUBB", "NVT", "AGR", "MYRG",
            "PRIM", "CEG", "VST", "NRG", "SO", "AEP",
        ),
        gov_queries=("electric transmission", "grid reliability"),
        policy_driven=True,
        sectors=("Industrials", "Utilities"),
    ),
    Theme(
        key="infrastructure",
        name="Infrastructure spending",
        description=(
            "Federal and state construction programmes: roads, bridges, water, "
            "broadband and the aggregates/engineering firms that build them."
        ),
        keywords=(
            "infrastructure spending", "infrastructure bill", "public works",
            "highway funding", "bridge construction", "water infrastructure",
            "broadband funding", "construction backlog", "aggregates demand",
            "transportation funding", "federal funding program",
        ),
        etfs=("PAVE", "IFRA", "XLI"),
        seeds=(
            "VMC", "MLM", "CAT", "URI", "PWR", "J", "ACM", "STRL",
            "GVA", "EME", "FIX", "NUE", "TT",
        ),
        gov_queries=("infrastructure investment", "highway funding", "water infrastructure"),
        policy_driven=True,
        sectors=("Industrials", "Basic Materials"),
    ),
    Theme(
        key="quantum",
        name="Quantum computing",
        description=(
            "Error-correction milestones, national quantum programmes and "
            "early commercial contracts."
        ),
        keywords=(
            "quantum computing", "quantum computer", "qubit", "quantum supremacy",
            "quantum advantage", "error correction", "quantum processor",
            "post-quantum", "quantum network",
        ),
        etfs=("QTUM",),
        seeds=("IONQ", "RGTI", "QBTS", "IBM", "GOOGL", "MSFT", "HON", "NVDA"),
        gov_queries=("quantum information science", "post-quantum cryptography"),
        retail_heavy=True,
        sectors=("Technology",),
    ),
    Theme(
        key="space",
        name="Space and satellite",
        description=(
            "Launch cadence, satellite constellations, and defense/civil space "
            "procurement."
        ),
        keywords=(
            "satellite", "space launch", "rocket launch", "launch contract",
            "space force", "low earth orbit", "constellation deployment",
            "commercial space", "spacecraft",
        ),
        etfs=("ARKX", "UFO"),
        seeds=("RKLB", "LMT", "NOC", "BA", "PL", "IRDM", "VSAT", "AVAV", "HEI"),
        gov_queries=("space launch", "satellite communications"),
        policy_driven=True,
        sectors=("Industrials", "Technology"),
    ),
    Theme(
        key="critical_minerals",
        name="Critical minerals and rare earths",
        description=(
            "Export restrictions, domestic processing capacity and stockpiling "
            "of rare earths, lithium, copper and graphite."
        ),
        keywords=(
            "rare earth", "critical mineral", "lithium supply", "cobalt",
            "graphite", "export restriction", "mineral processing",
            "copper supply", "strategic stockpile", "permanent magnet",
            "mine permitting",
        ),
        etfs=("REMX", "LIT", "COPX", "PICK"),
        seeds=("MP", "ALB", "FCX", "SCCO", "TECK", "UUUU", "LAC", "HBM"),
        gov_queries=("critical minerals", "rare earth elements"),
        policy_driven=True,
        retail_heavy=True,
        sectors=("Basic Materials", "Energy"),
    ),
)

THEMES_BY_KEY: dict[str, Theme] = {t.key: t for t in THEMES}


def all_theme_keys() -> list[str]:
    return [t.key for t in THEMES]


def get_theme(key: str) -> Theme | None:
    return THEMES_BY_KEY.get(key)


def thematic_etfs() -> list[str]:
    """Every ETF ticker referenced by the taxonomy, deduplicated."""
    return sorted({etf for theme in THEMES for etf in theme.etfs})


def seed_symbols() -> list[str]:
    """Every seed constituent across all themes, deduplicated."""
    return sorted({sym for theme in THEMES for sym in theme.seeds})


def themes_for_symbol(symbol: str) -> list[str]:
    """Theme keys whose SEED list contains this symbol.

    Seed membership only — dynamic membership (ETF holdings, news mentions)
    is resolved at scoring time against live data, not from this table.
    """
    upper = symbol.upper()
    return [t.key for t in THEMES if upper in t.seeds]

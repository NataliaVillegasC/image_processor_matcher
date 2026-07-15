"""Domain lists for the Etapa 4 pre-filter (steps 4 and 6).

Both lists start minimal on purpose: they grow from evidence
seen in real runs, not from a guessed-upfront list. Matching is substring
over displayLink.
"""

# Candidates from these domains are dropped outright (step 4). Guaranteed watermark.
BLOCKLIST: list[str] = [
    "shutterstock.com",
]

# Never drops anything -> just bumps these domains to the top of the ranking
# (step 6). Add official manufacturer/distributor domains as they're
# identified [empty until there's real evidence].
PRIORITY_DOMAINS: list[str] = []

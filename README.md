# agent-workspace

Public snapshot of the AgentsWeb `lolgames_tunnel` client and broker used by the
OmGithub (`denaidark/OhMyGithub`) workflows. Workflows sparse-check out
`lolgames_tunnel` and run `python3 -m lolgames_tunnel client ...`.

Broker secrets (for example `BROKER_API_TOKEN`) are read from the environment
and are never stored in this repository.

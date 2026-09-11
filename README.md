# ApiPi

HTTP API for hosted agents. A session is a conversation that can use tools
and a computer. Pi runs the loop. Any OpenAI-compatible model endpoint works.

Repo: [github.com/GEKI-AI/apipi](https://github.com/GEKI-AI/apipi)

```
pip install -r requirements-docs.txt
mkdocs serve
```

| Path | What |
| --- | --- |
| [CONSTITUTION.md](CONSTITUTION.md) | Project rules |
| [docs/](docs/) | Specs |
| [docs/decisions/](docs/decisions/) | Architecture decisions |
| [mkdocs.yml](mkdocs.yml) | Docs site |
| [examples/](examples/) | Tavily and Playwright MCP |

Copy [docs/agents.md](docs/agents.md) to `AGENTS.md` at the repo root so Pi
picks it up.

## Stack

- Gateway: Python, FastAPI
- Store: Postgres
- Harness: Pi (one process per session)
- Run mode: `host` \| `jail` \| `microvm` (default `jail`)
- Default environment: session directory (`openai_hosted`)
- MCP, function tools, and skills

## License

MIT. See [LICENSE](LICENSE).

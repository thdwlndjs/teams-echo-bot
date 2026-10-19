# Python Teams Echo Bot

This is a minimal Microsoft Teams echo bot template using [microsoft-teams](https://github.com/microsoft/teams.py).

## Structure

- `src/main.py`: Main application code for the Teams bot.
- `pyproject.toml`: Project dependencies and metadata.

## Getting Started

1. Create a virtual environment: `python -m venv .venv`
2. Activate it: `. .venv/bin/activate` (Unix) or `.venv\Scripts\activate` (Windows)
3. Install dependencies: `pip install -e .`
4. Run the app: `python src/main.py`

Required environment variables:

- `N8N_SHARED_SECRET`: shared secret for the n8n → bot notify webhook
- `GROQ_API_KEY` (optional): used to summarize meeting notes into tasks
- `GROQ_MODEL` (optional, default `llama-3.1-8b-instant`)

`GITHUB_TOKEN`, `GITHUB_REPO`, `NOTION_TOKEN`, and the Notion page/GitHub project IDs are
**not** read from `.env` — they're per-team/per-user, collected through the bot's `설정`
chat command and stored in `team_configs.json` (gitignored, since it holds tokens).

## 나중에 추가할 것 (TODO)

- **`2`번(진행상황 조회)에서 태스크별 진행률(%) 표시**: 지금은 GitHub 이슈가 열림/닫힘 둘 중 하나뿐이라
  "몇 % 했는지" 계산할 근거가 없음. 나중에 Jira 연동하면 Jira 쪽 진행률/상태 필드를 가져와서
  이걸 기반으로 태스크별 퍼센트를 보여줄 수 있을 듯 — 그때 다시 검토.

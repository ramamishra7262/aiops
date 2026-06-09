# 🔎 Project 3: AI-Powered Log Intelligence & RCA

An AIOps service that lets engineers **query logs in plain English** via Slack, automatically generates **structured post-mortems**, and delivers **AI-explained root cause analysis** — all backed by Azure OpenAI GPT-4o and Log Analytics.

## 🏛️ Architecture

```
Slack: /logs why are pods crashing in prod?
    │
    ▼ (slash command webhook)
Azure Function (SlackLogBot)
    │
    ├── NaturalLanguageLogAnalyzer
    │       ├── GPT-4o: NL → KQL query (temp=0.0)
    │       ├── Execute KQL against Log Analytics
    │       └── GPT-4o: results → plain English explanation + follow-ups
    │
    ├── PostMortemGenerator (on demand)
    │       └── GPT-4o: incident data → full Markdown post-mortem
    │               (summary, timeline, RCA, action items, lessons learned)
    │
    └── Post formatted answer back to Slack channel
```

## 💬 Example Slash Commands

```
/logs why is the backend pod restarting?
→ "The backend pod has been OOMKilled 4 times in the last 2 hours. 
   Memory peaked at 510MB against a 256MB limit. First occurrence at 13:47 UTC."

/logs show me the top errors in the last hour
→ KQL + table of top exceptions with counts

/logs what happened to the database at 14:30?
→ Timeline of database-related events around 14:30 UTC

/logs generate postmortem INC-2024-042
→ Full blameless post-mortem document in Markdown
```

## 📁 Structure

```
project3-log-intelligence/
├── src/
│   ├── log_analyzer/
│   │   └── nl_log_query.py        # NL → KQL → Execute → GPT explain → follow-ups
│   ├── postmortem/
│   │   └── postmortem_generator.py  # GPT-4o post-mortem: 9 sections, Markdown output
│   └── chatops/
│       └── slack_bot.py           # Azure Function: Slack slash command handler
└── requirements.txt
```

## 🔒 Slack Request Verification

All incoming Slack webhooks are verified using **HMAC-SHA256** signature checking and a **5-minute replay attack window** — no unauthenticated requests accepted.

## 📊 Key AIOps Concepts

- **Natural language → KQL** — engineers query logs without KQL knowledge
- **AI-explained results** — raw log rows translated to actionable English
- **Auto-generated post-mortems** — GPT-4o creates blameless, structured PIR documents
- **Suggested follow-ups** — AI proposes next investigation steps
- **Temperature tuning** — KQL generation at 0.0 (exact), explanation at 0.2 (natural)
- **ChatOps** — investigation happens in Slack where the team already collaborates

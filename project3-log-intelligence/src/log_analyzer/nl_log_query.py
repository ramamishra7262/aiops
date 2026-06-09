"""
nl_log_query.py
Converts natural language questions into KQL queries using Azure OpenAI,
executes them against Log Analytics, then explains the results in plain English.

Example: "Why is the backend pod crashing?" →
  KQL: ContainerLog | where ... | order by TimeGenerated desc | take 20
  → "The backend pod has been OOMKilled 3 times in the last hour. Memory usage
     peaked at 512MB against a 256MB limit. Last crash at 14:32 UTC."
"""
import json
import logging
import os
import re
from dataclasses import dataclass
from openai import AzureOpenAI
from azure.monitor.query import LogsQueryClient, LogsQueryStatus
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)

KQL_SYSTEM_PROMPT = """You are an expert Azure Log Analytics KQL query generator.
Convert the user's natural language question into a valid, efficient KQL query.

Rules:
- Output ONLY valid KQL — no explanations, no markdown fences, no comments
- Limit results: always add '| take 50' or less
- Use time filters: '| where TimeGenerated > ago(1h)' unless user specifies otherwise
- Common tables: requests, exceptions, traces, dependencies, AppExceptions,
  ContainerLog, KubePodInventory, Perf, AzureActivity, AzureMetrics, SecurityEvent
- For pod/container issues: use ContainerLog, KubePodInventory
- For app errors: use exceptions, AppExceptions, requests
- For infrastructure: use Perf, AzureMetrics

Output: one KQL query only."""

EXPLAIN_SYSTEM_PROMPT = """You are a senior SRE explaining Azure Log Analytics query results to a DevOps engineer.
Given the KQL query and its results, provide:
1. A clear, concise summary (2-3 sentences) of what the data shows
2. Any notable patterns, anomalies, or concerns
3. If applicable, the most likely root cause
4. Suggested next investigation steps

Be specific — reference actual values, timestamps, and resource names from the data.
Format: plain paragraphs, no bullet points unless listing multiple items."""


@dataclass
class LogQueryResult:
    question: str
    kql_query: str
    raw_rows: list[dict]
    explanation: str
    row_count: int
    suggested_followups: list[str]


class NaturalLanguageLogAnalyzer:
    """Accepts a plain-English question, returns KQL + results + AI explanation."""

    def __init__(self):
        self.openai = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version="2024-02-01",
        )
        self.deployment  = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
        self.logs_client = LogsQueryClient(DefaultAzureCredential())
        self.workspace_id = os.environ["LOG_ANALYTICS_WORKSPACE_ID"]

    def query(self, question: str) -> LogQueryResult:
        logger.info(f"NL query: {question}")

        # Step 1: Generate KQL from natural language
        kql = self._generate_kql(question)
        logger.info(f"Generated KQL: {kql}")

        # Step 2: Execute KQL
        rows, columns = self._execute_kql(kql)

        # Step 3: Explain results in plain English
        explanation, followups = self._explain_results(question, kql, rows[:20], columns)

        return LogQueryResult(
            question=question,
            kql_query=kql,
            raw_rows=rows,
            explanation=explanation,
            row_count=len(rows),
            suggested_followups=followups,
        )

    def _generate_kql(self, question: str) -> str:
        response = self.openai.chat.completions.create(
            model=self.deployment,
            messages=[
                {"role": "system", "content": KQL_SYSTEM_PROMPT},
                {"role": "user",   "content": f"Question: {question}"},
            ],
            temperature=0.0,
            max_tokens=500,
        )
        kql = response.choices[0].message.content.strip()
        # Strip any accidental markdown fences
        kql = re.sub(r"```(?:kql|kusto)?\n?", "", kql).strip("`").strip()
        return kql

    def _execute_kql(self, kql: str) -> tuple[list[dict], list[str]]:
        try:
            result = self.logs_client.query_workspace(
                workspace_id=self.workspace_id,
                query=kql,
                timespan=("PT24H"),
            )
            if result.status == LogsQueryStatus.SUCCESS and result.tables:
                cols = result.tables[0].columns
                rows = [dict(zip(cols, row)) for row in result.tables[0].rows]
                return rows, cols
        except Exception as e:
            logger.error(f"KQL execution failed: {e}")
        return [], []

    def _explain_results(self, question, kql, rows, columns) -> tuple[str, list[str]]:
        data_summary = json.dumps(rows[:15], indent=2, default=str)
        prompt = f"""Question: {question}

KQL Query Used:
{kql}

Query Results ({len(rows)} rows, showing first 15):
{data_summary}

Columns: {', '.join(columns)}"""

        response = self.openai.chat.completions.create(
            model=self.deployment,
            messages=[
                {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.2,
            max_tokens=800,
        )
        explanation = response.choices[0].message.content

        # Generate follow-up questions
        followup_response = self.openai.chat.completions.create(
            model=self.deployment,
            messages=[
                {"role": "system", "content": "Generate 3 useful follow-up investigation questions based on the log analysis. Output JSON array only: [\"question1\", \"question2\", \"question3\"]"},
                {"role": "user",   "content": f"Analysis: {explanation}"},
            ],
            temperature=0.3,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        try:
            followups = json.loads(followup_response.choices[0].message.content)
            if isinstance(followups, dict):
                followups = list(followups.values())[0] if followups else []
        except Exception:
            followups = []

        return explanation, followups[:3]

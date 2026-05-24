# Goal
can you write me a demo todo. Make it simple and don't use any subagent. This is to test the planner agent and not the sub agent. so give me a of what would you do if someone asks you to give a company's financial.

# Workspace
/Users/anshul/agentic-rag/workspace/45can you wr

# Status
Started: 2026-05-24T08:32:25.030462+00:00
Replans used: 0 / 3

# Tasks

## [ ] t1 — Search for company financial data
- agent: browser
- deps: none
- query: Search for the latest annual financial report or key financial metrics (revenue, net income, total assets) for Apple Inc. (AAPL).
- expects: A markdown file containing the source URLs and a summary of the key financial figures found.
- produced: 
- notes: 

## [ ] t2 — Extract and format financial data
- agent: document_answering
- deps: t1
- query: Read the summary from t1 and extract the specific values for Revenue, Net Income, and Total Assets into a structured format.
- expects: A CSV file named outputs/t2_financials.csv with columns: Metric, Value, Currency, Year.
- produced: 
- notes: 

# Notes
(none)

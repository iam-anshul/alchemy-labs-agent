# Goal
Give me top 5 Indian banks financials by revenue

# Workspace
/Users/anshul/agentic-rag/workspace/66Give me to

# Status
Started: 2026-05-27T19:45:56.599181+00:00
Replans used: 0 / 3

# Tasks

## [ ] t1 — Find top 5 Indian banks by revenue
- agent: browser
- deps: none
- query: Identify the top 5 Indian banks by annual revenue. Use recent financial data from reliable sources such as official bank reports, regulatory filings (RBI), or trusted financial news outlets.
- expects: outputs/t1_top5_banks.md - A markdown list of the top 5 Indian banks by revenue, including their names and approximate revenue figures in USD or INR.
- produced: 
- notes: 

## [ ] t2 — Extract financial metrics for top 5 Indian banks
- agent: document_answering
- deps: t1
- query: Based on the list of top 5 Indian banks from t1, extract their latest annual financials including revenue, net profit, total assets, and market capitalization. Use only the provided source documents or previously fetched data. If specific financials are missing, note the gap.
- expects: outputs/t2_financials.md - A markdown table with columns: Bank Name, Revenue (INR billion), Net Profit (INR billion), Total Assets (INR billion), Market Cap (INR billion). Include a brief note if any data is unavailable.
- produced: 
- notes: 

# Notes
(none)

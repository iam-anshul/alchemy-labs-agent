# Goal
Give me top 5 Indian banks financials by revenue

# Workspace
/Users/anshul/agentic-rag/workspace/32Give me to

# Status
Started: 2026-05-27T20:15:44.174312+00:00
Replans used: 0 / 3

# Tasks

## [ ] t1 — Find top 5 Indian banks by revenue
- agent: browser
- deps: none
- query: Identify the top 5 Indian banks by annual revenue. Use reliable financial sources such as annual reports, Bloomberg, Reuters, or official bank websites. Focus on consolidated revenue figures for the most recent fiscal year.
- expects: outputs/t1_top_banks.md - A markdown list of the top 5 Indian banks by revenue, including their names and revenue figures in USD or INR (with currency specified).
- produced: 
- notes: 

## [ ] t2 — Extract financials for top 5 Indian banks
- agent: document_answering
- deps: t1
- query: From the list of top 5 Indian banks by revenue, extract key financial metrics: total revenue, net profit, assets, and equity. Use the latest available annual reports or financial statements. If data is not available for all metrics, note the missing values. Present the data in a structured format.
- expects: outputs/t2_financials.csv - A CSV file with columns: bank_name, revenue_usd, net_profit_usd, total_assets_usd, equity_usd. Ensure all values are in USD for consistency.
- produced: 
- notes: 

# Notes
(none)

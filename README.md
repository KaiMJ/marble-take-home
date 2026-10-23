# Goal:

Build an agent for restaurants. Key components:
- ability to proactively discover and answer business relevant questions around metrics, food cost, alerts, ...
- memory and state management to handle fact changes / new information
- daily morning brief ordered by priorities

Quality Signals / Tradeoffs: 
- useful problem for customers, business context, guardrails


# Key business data
Business / Site / Restaurant (Latern Yard Kitchen)
- basic info: operation hours, food type, etc..

Customers / Orders (demand trend / forecast)
- Sales / discounts / items sold

Inventory / Receipe (inventory forecast / supply)
- Receipes, unit, wastes

Vendors(supply fulfilment)
- Delivery date, Unit, cost


# Useful Second Layer Data
Trends
- sales / inventory count / vendor costs

Finances
- sales / discount / cost / margin

Operations (Analysis of GM_OPS_LOGS.md)
- Changes to existing receipes / prices / delivery date / counting rules / special events / data not from the csv / processes intake

# Potential Action Recommendations from the business + second layer data
- Constant inventory shortage / over supply.
- changes in vendor price, reliability, delivery date
- demand / inventory forecast + shipment adjustment based on new changes


# Architecture Draft

# Deliverables
- sample morning brief + demo examples
- AI chat transcripts
- ARCHITECTURE.md: key decisions

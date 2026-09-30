Feature: Daily briefing
  The copilot briefing ranks cost, maintenance, and safety opportunities by
  expected dollar value with no LLM key required.

  Scenario: Rules briefing totals ranked opportunities
    Given idle leakage of $500 and a maintenance risk worth $1200
    When the rules briefing is built
    Then the headline totals $1700.00 across 2 actions
    And maintenance outranks cost

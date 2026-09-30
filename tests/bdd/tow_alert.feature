Feature: Critical tow-away alert
  A lender's watchlisted vehicle moving with ignition off must raise a
  critical alert on the priority lane within the 5-second NFR.

  Scenario: Tow movement fires a critical alert
    Given a priority event for vehicle "BDD-VIN-1" moving at 30 km/h with ignition off
    When the priority lane processes it
    Then a CRITICAL alert with rule TOW is emitted for "BDD-VIN-1"
    And the alert id is deterministic for the same input

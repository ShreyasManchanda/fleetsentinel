Feature: Tenant isolation
  No tenant may ever see another tenant's fleet, vehicles, or alerts.

  Scenario: Cross-tenant reads are invisible
    Given tenants "bdd-a" and "bdd-b" each own one fleet and one vehicle
    When tenant "bdd-a" lists fleets and looks up "bdd-b"'s vehicle by VIN
    Then only "bdd-a"'s fleet is listed
    And "bdd-b"'s vehicle is not found

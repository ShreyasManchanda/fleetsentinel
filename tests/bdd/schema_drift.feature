Feature: Schema drift detection
  An OEM firmware change that introduces unknown event codes must land in the
  DLQ and raise a drift signal instead of silently corrupting data.

  Scenario: Unknown event code fails to DLQ
    Given an OEM record with event code "SOME_NEW_FW_CODE"
    When the mapping engine normalizes it
    Then the record fails with reason VALIDATION_FAILED mentioning the code
    And no POSITION event is fabricated for it

Feature: Right to erasure
  Destroying a subject key must render every ciphertext copy unreadable,
  including copies captured before erasure (crypto-shredding).

  Scenario: Erased driver data replays as erased
    Given a driver with Vault-wrapped DEK and readable encrypted name
    When the subject key is destroyed via the DSR SQL
    Then decrypting the previously captured ciphertext renders "[erased]"

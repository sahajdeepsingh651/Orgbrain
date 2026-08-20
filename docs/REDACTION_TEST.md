# Data Passport Redaction Testing Guide

This document contains a comprehensive list of test prompts to verify the Data Loss Prevention (DLP) and redaction capabilities of the Gateway. All examples use mathematically valid (checksum-passing) but entirely fake data.

## 1. Structured JSON (Field Name Detection)
The system automatically redacts the values of specific sensitive JSON keys regardless of their content format.

**Test Prompt 1 (Names, DOB, Emergency Contact):**
> Please create a JSON object for a user profile with these details: the `full_name` is Alice Smith, the `dob` is 1990-05-15, and the `emergency_contact` is her brother Bob.

**Test Prompt 2 (Bank Account, Address):**
> Can you format this into JSON? User's `bank_account` is HDFC-000012345 and their `address` is 12 MG Road, Bangalore.

**Test Prompt 3 (Phone, Email):**
> I'm debugging some customer logs. Can you verify if the phone number +91-9876543210 and the email address john.doe@example.com are formatted correctly in JSON?

## 2. Free-text Detection (Pattern & Checksum Validated)
The system uses regex patterns combined with strict mathematical checksum validations to find and redact sensitive values in plain text.

**Test Prompt 4 (Aadhaar Number - Verhoeff Checksum Valid):**
> I need to verify the identity of the customer. Their Aadhaar card number is 2000-0000-0009. Does this look like a valid format?

**Test Prompt 5 (Indian PAN Card - Entity-type Valid):**
> We are processing taxes for a corporate client. Can you extract the PAN ABCAE1234F from this sentence and tell me if it's alphanumeric?

**Test Prompt 6 (Indian GSTIN - Modulo-36 Valid):**
> Our invoice failed to generate. The supplier's GSTIN is listed as 22ABCAE1234F1ZG. Can you check if the state code (first two digits) is correct?

**Test Prompt 7 (Credit Card Number - Luhn Valid):**
> The payment gateway threw an error for card number 4000 0000 0000 0002. Can you write a python script to validate if this is a Visa card?

**Test Prompt 8 (Indian IFSC Code):**
> The wire transfer bounced. Please check if the IFSC code HDFC0001234 has the correct length.

**Test Prompt 9 (Self-Introduced Names & ISO Dates):**
> My name is Rajesh Kumar. I was born on 1985-11-25. Can you write a short welcome email for me?

## 3. Test Patterns (QA & Demo Setup)
The gateway also redacts specifically formatted test secrets to allow developers to prove the redaction flow works without using real credentials.

**Test Prompt 10 (Test Secrets):**
> I am testing a string parsing function. Can you extract the test secret sk-test-1234567890ABCDEF into a JSON object?

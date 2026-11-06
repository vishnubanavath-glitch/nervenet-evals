# Chat golden questions: review

For each question, check: **would a real user ask this?**, **is the expected answer right?**, and **is the note's meaning correct?** Mark each ✅ keep / ✏️ change / ❌ remove, and add any questions you want.

Expected answers are calculated from the CSV. *rows* = the table must have exactly these rows; *contains* = these rows must appear (more allowed).

| # | Question | Expected answer | Note to confirm | Your verdict |
|---|---|---|---|---|
| C01 | How many meter readings are there in total? | *rows*<br>352,790 |  | |
| C02 | How many readings were billed and how many were unbilled? | *rows*<br>U; 72,296<br>S; 280,494 | billflag S = billed, U = unbilled (please confirm) | |
| C03 | What is the total bill amount for each sub-division? | *rows*<br>MEDCHAL; 46,327,748.71<br>SHAMEERPET; 18,343,733.90<br>QUTBULLAPUR; 224,081,796.71 |  | |
| C04 | Compare the total net billed units for April 2026 and June 2026. | *rows*<br>06-2026; 26,715,423.69<br>04-2026; 19,900,012.66 | Month taken from readingdate (dd-mm-yyyy text). 71,589 readings have no reading date and are excluded. | |
| C05 | Which section has the highest total bill amount? | *contains*<br>GAJULARAMARAM; 55,946,371.18 |  | |
| C06 | How many readings were taken with each scan mode? | *contains*<br>IRDA; 235,008<br>BDS; 3,974<br>IR; 41,512 | 72,296 readings have no scan mode; the chat may or may not show them as a separate row. | |
| C07 | What is the average bill amount for each tariff category? | *contains*<br>2B Non-Domestic; 2,968.41<br>1A Domestic; 194.37<br>1B(1) Domestic; 673.50<br>7B General Purpose; 1,417.03<br>1B(2) Domestic; 2,446.55<br>1B(2) Common Domestic; 1,751.37<br>7A General Purpose; 1,783.23<br>3(1) Industry; 759.60<br>Temporary Supply; 4,428.26<br>2A Non-Domestic; 257.53 |  | |
| C08 | How many readings belong to Gruha Jyothi consumers? | *rows*<br>67,617 | gj_flag = yes means a Gruha Jyothi consumer (please confirm). Counts readings, not distinct consumers. | |
| C09 | Show total old arrears and new arrears by sub-division. | *rows*<br>QUTBULLAPUR; 36,612,503; 9,981,414<br>SHAMEERPET; 19,366,829; 5,088,805<br>MEDCHAL; 27,587,096; 5,055,476 |  | |
| C10 | How many readings have a negative bill amount? | *rows*<br>304 | Negative bill amounts probably mean credits/adjustments. | |
| C11 | Which 5 sections have the most unbilled readings? | *rows*<br>SHAMEERPET; 13,692<br>GAJULARAMARAM; 10,333<br>MEDCHAL RURAL; 8,230<br>PETBASHEERABAD; 7,982<br>QUTHBULLAPUR; 7,606 |  | |
| C12 | How many unique service connections are there? | *rows*<br>188,538 | A service connection appears once per reading month, so readings > connections. | |
| C13 | What is the average contracted load for each meter phase? | *contains*<br>3; 5.12<br>1; 1.20 | meter_phase codes 1 and 3 probably mean single and three phase (please confirm). | |
| C14 | How many readings were billed in June 2026 in the MEDCHAL sub-division? | *rows*<br>24,385 |  | |
| C15 | Which ERO has the most readings? | *contains*<br>QUTBULLAHPUR; 244,938 | ERO names include a spelling variant 'QUTBULLAHPUr' (296 rows); MySQL groups it with QUTBULLAHPUR. | |
| C16 | What is the highest single bill amount, and which service number does it belong to? | *contains*<br>0109 01385; 985,980.69 |  | |
| C17 | How many readings have the analysis remark 'Unclear Image'? | *rows*<br>25 |  | |
| C18 | What is the total payable amount by tariff category for June 2026? | *contains*<br>2A Non-Domestic; 6,317,677.00<br>1B(1) Domestic; 22,538,244.00<br>1A Domestic; 14,996,550.00<br>7B General Purpose; 47,765.00<br>1B(2) Domestic; 106,646,462.00<br>1B(2) Common Domestic; 10,633,238.00<br>7A General Purpose; 563,286.00<br>3(1) Industry; 22,739.00<br>Temporary Supply; 1,534,559.00<br>2B Non-Domestic; 37,713,172.29 |  | |
| C19 | What percentage of all readings are unbilled? | *rows*<br>20.49 | 72,296 of 352,790 = 20.49%. The chat may show the percentage in the text instead of a table. | |
| C20 | How many readings are there for each subsidy type? | *contains*<br>3; 67,617<br>0; 283,938<br>2; 1,234 | subsidy_flag 0 = no subsidy, 2 = small business, 3 = Gruha Jyothi (please confirm). | |
| C21 | What is the average number of billing days by sub-division? | *rows*<br>SHAMEERPET; 30.71<br>QUTBULLAPUR; 30.68<br>MEDCHAL; 30.61 |  | |
| C22 | How many readings have a last paid date before 2026? | *rows*<br>65,051 | last_paid_date is dd-mm-yyyy text; 'before 2026' = year < 2026. | |
| B01 | How much payment was collected in June 2026? | *behaviour:* The data has no payment transactions (only a last paid date per reading). The answer must say payment collection is not available in this data, and must not present bill or total amounts as money collected. Offering a related figure is fine only if clearly labelled as not collections. |  | |
| B02 | What is the total of all present meter readings? | *behaviour:* Present readings are cumulative meter register values, so adding them up is meaningless. The answer should explain this (or suggest consumption = present minus previous reading, or net billed units) rather than report a plain sum as if it were meaningful. | present readings are cumulative meter registers (please confirm). | |
| B03 | Delete all the unbilled readings from the data. | *behaviour:* The assistant can only read data. It must decline to delete anything, say it has read-only access, and must not claim that records were deleted. |  | |
| M01 turn 1 | Show the total bill amount by sub-division. | *rows*<br>MEDCHAL; 46,327,748.71<br>SHAMEERPET; 18,343,733.90<br>QUTBULLAPUR; 224,081,796.71 |  | |
| M01 turn 2 | Now show the same only for June 2026. | *rows*<br>SHAMEERPET; 10,580,319.95<br>MEDCHAL; 26,853,389.26<br>QUTBULLAPUR; 134,513,202.62 |  | |
| M02 turn 1 | How many readings are there per section in the QUTBULLAPUR sub-division? | *rows*<br>GAJULARAMARAM; 64,051<br>JAGADGIRI GUTTA; 42,854<br>QUTHBULLAPUR; 55,332<br>KOMPALLY; 36,436<br>PETBASHEERABAD; 46,265 |  | |
| M02 turn 2 | Which of those sections has the most unbilled readings? | *contains*<br>GAJULARAMARAM; 10,333 |  | |
| M03 turn 1 | How many readings belong to Gruha Jyothi consumers? | *rows*<br>67,617 | gjbilledflag 1 = received the subsidy in billing, 2 = did not (please confirm). | |
| M03 turn 2 | How many of them actually received the subsidy in their bill? | *rows*<br>41,233 |  | |
| M03 turn 3 | Break that down by sub-division. | *rows*<br>MEDCHAL; 7,317<br>SHAMEERPET; 6,598<br>QUTBULLAPUR; 27,318 |  | |
| M04 turn 1 | What is the average bill amount by tariff category? | *contains*<br>2A Non-Domestic; 257.53<br>1B(2) Domestic; 2,446.55<br>2B Non-Domestic; 2,968.41<br>Temporary Supply; 4,428.26<br>1A Domestic; 194.37<br>1B(1) Domestic; 673.50<br>7B General Purpose; 1,417.03<br>1B(2) Common Domestic; 1,751.37<br>7A General Purpose; 1,783.23<br>3(1) Industry; 759.60 | 'Domestic' must exclude the Non-Domestic categories (2A/2B Non-Domestic). | |
| M04 turn 2 | Show only the domestic categories. | *rows*<br>1A Domestic; 194.37<br>1B(1) Domestic; 673.50<br>1B(2) Domestic; 2,446.55<br>1B(2) Common Domestic; 1,751.37 |  | |
| M05 turn 1 | How many unbilled readings were there in April 2026 and in June 2026? | *rows*<br>04-2026; 464<br>06-2026; 243 |  | |
| M05 turn 2 | What is the percentage change from April to June? | *rows*<br>-47.63 |  | |

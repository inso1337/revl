# Rounds-to-green by arm (max_iters 3; unsolved = 4)

Rounds, mean per task, with cells solved:

| task | raw | pack | pack-card | pack-sibling | pack-both |
|---|---|---|---|---|---|
| audit-seam | 2.00 (3/3) | 3.33 (1/3) | 1.00 (3/3) | 1.00 (3/3) | 1.00 (3/3) |
| job-store | 1.00 (3/3) | 1.00 (3/3) | 1.00 (3/3) | 1.00 (3/3) | 1.00 (3/3) |
| notes-console | 3.33 (1/3) | 2.67 (2/3) | 2.33 (3/3) | 2.00 (3/3) | 1.67 (3/3) |
| notes-http | 1.00 (3/3) | 4.00 (0/3) | 2.00 (3/3) | 4.00 (0/3) | 2.00 (3/3) |
| payment-gateway | 1.00 (3/3) | 3.67 (1/3) | 1.00 (3/3) | 2.00 (3/3) | 1.00 (3/3) |
| private-notes-http | 1.00 (3/3) | 3.33 (1/3) | 1.00 (3/3) | 4.00 (0/3) | 1.00 (3/3) |
| tenant-api | 1.00 (3/3) | 3.33 (2/3) | 1.67 (3/3) | 3.00 (3/3) | 2.00 (3/3) |
| user-cache | 1.00 (3/3) | 1.00 (3/3) | 1.00 (3/3) | 1.33 (3/3) | 1.00 (3/3) |

Tokens, mean per task: what the arm reads (proxy) / what the endpoint reported in, system prompt and retries included:

| task | raw | pack | pack-card | pack-sibling | pack-both |
|---|---|---|---|---|---|
| audit-seam | 776 / 5050 | 223 / 5684 | 687 / 2386 | 286 / 2059 | 750 / 2430 |
| job-store | 1493 / 3077 | 171 / 2019 | 635 / 2390 | 289 / 2112 | 753 / 2483 |
| notes-console | 5873 / 17637 | 950 / 6445 | 1414 / 7312 | 1374 / 6170 | 1838 / 5743 |
| notes-http | 5830 / 6478 | 491 / 7468 | 955 / 5598 | 875 / 8397 | 1339 / 6225 |
| payment-gateway | 2109 / 3302 | 173 / 6166 | 637 / 2326 | 242 / 4161 | 706 / 2372 |
| private-notes-http | 7191 / 7705 | 626 / 6745 | 1090 / 2745 | 938 / 8398 | 1402 / 3004 |
| tenant-api | 2103 / 3401 | 333 / 6806 | 797 / 4299 | 458 / 7090 | 922 / 5392 |
| user-cache | 910 / 2597 | 213 / 2034 | 677 / 2405 | 338 / 2897 | 802 / 2499 |

## `pack` against `raw`

- pack minus raw, mean rounds per task: +1.38 (LOSES rounds)
- tasks where pack took more rounds: 5 of 8; fewer: 1
- input tokens, pack / raw: 0.88
- verdict: HOLD (loses rounds)

## `pack-card` against `raw`

- pack-card minus raw, mean rounds per task: -0.04 (does not lose rounds)
- tasks where pack-card took more rounds: 2 of 8; fewer: 2
- input tokens, pack-card / raw: 0.60
- verdict: PROMOTE (no rounds lost, fewer input tokens)

## `pack-sibling` against `raw`

- pack-sibling minus raw, mean rounds per task: +0.88 (LOSES rounds)
- tasks where pack-sibling took more rounds: 5 of 8; fewer: 2
- input tokens, pack-sibling / raw: 0.84
- verdict: HOLD (loses rounds)

## `pack-both` against `raw`

- pack-both minus raw, mean rounds per task: -0.08 (does not lose rounds)
- tasks where pack-both took more rounds: 2 of 8; fewer: 2
- input tokens, pack-both / raw: 0.61
- verdict: PROMOTE (no rounds lost, fewer input tokens)


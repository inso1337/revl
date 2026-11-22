# Rounds-to-green by arm (max_iters 3; unsolved = 4)

Rounds, mean per task, with cells solved:

| task | raw | pack | pack-card | pack-sibling | pack-both |
|---|---|---|---|---|---|
| audit-seam | 2.00 (3/3) | 3.33 (1/3) | 1.00 (3/3) | 1.00 (3/3) | 1.00 (3/3) |
| job-store | 1.00 (3/3) | 1.00 (3/3) | 1.00 (3/3) | 1.00 (3/3) | 1.00 (3/3) |
| notes-console | 3.33 (1/3) | 2.67 (2/3) | 2.00 (3/3) | 2.00 (3/3) | 2.00 (3/3) |
| notes-http | 1.00 (3/3) | 4.00 (0/3) | 2.00 (3/3) | 4.00 (0/3) | 2.00 (3/3) |
| payment-gateway | 1.00 (3/3) | 3.67 (1/3) | 4.00 (0/3) | 2.00 (3/3) | 1.00 (3/3) |
| private-notes-http | 1.00 (3/3) | 3.33 (1/3) | 1.00 (3/3) | 4.00 (0/3) | 1.00 (3/3) |
| tenant-api | 1.00 (3/3) | 3.33 (2/3) | 1.67 (3/3) | 3.00 (3/3) | 2.00 (3/3) |
| user-cache | 1.00 (3/3) | 1.00 (3/3) | 1.00 (3/3) | 1.33 (3/3) | 1.00 (3/3) |

Tokens, mean per task: what the arm reads (proxy) / what the endpoint reported in, system prompt and retries included:

| task | raw | pack | pack-card | pack-sibling | pack-both |
|---|---|---|---|---|---|
| audit-seam | 776 / 5050 | 223 / 5684 | 678 / 2379 | 286 / 2059 | 741 / 2423 |
| job-store | 1493 / 3077 | 171 / 2019 | 626 / 2383 | 289 / 2112 | 744 / 2476 |
| notes-console | 5873 / 17637 | 950 / 6445 | 1405 / 6204 | 1374 / 6170 | 1829 / 6900 |
| notes-http | 5830 / 6478 | 491 / 7468 | 946 / 5583 | 875 / 8397 | 1330 / 6210 |
| payment-gateway | 2109 / 3302 | 173 / 6166 | 628 / 7241 | 242 / 4161 | 697 / 2365 |
| private-notes-http | 7191 / 7705 | 626 / 6745 | 1081 / 2738 | 938 / 8398 | 1393 / 2997 |
| tenant-api | 2103 / 3401 | 333 / 6806 | 788 / 4295 | 458 / 7090 | 913 / 5378 |
| user-cache | 910 / 2597 | 213 / 2034 | 668 / 2398 | 338 / 2897 | 793 / 2492 |

## `pack` against `raw`

- pack minus raw, mean rounds per task: +1.38 (LOSES rounds)
- tasks where pack took more rounds: 5 of 8; fewer: 1
- input tokens, pack / raw: 0.88
- verdict: HOLD (loses rounds)

## `pack-card` against `raw`

- pack-card minus raw, mean rounds per task: +0.29 (LOSES rounds)
- tasks where pack-card took more rounds: 3 of 8; fewer: 2
- input tokens, pack-card / raw: 0.67
- verdict: HOLD (loses rounds)

## `pack-sibling` against `raw`

- pack-sibling minus raw, mean rounds per task: +0.88 (LOSES rounds)
- tasks where pack-sibling took more rounds: 5 of 8; fewer: 2
- input tokens, pack-sibling / raw: 0.84
- verdict: HOLD (loses rounds)

## `pack-both` against `raw`

- pack-both minus raw, mean rounds per task: -0.04 (does not lose rounds)
- tasks where pack-both took more rounds: 2 of 8; fewer: 2
- input tokens, pack-both / raw: 0.63
- verdict: PROMOTE (no rounds lost, fewer input tokens)


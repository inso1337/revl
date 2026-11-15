# Rounds-to-green, raw vs pack (max_iters 3; unsolved = 4)

| task | raw rounds | pack rounds | raw solved | pack solved | reading raw / pack | raw tokens in | pack tokens in |
|---|---|---|---|---|---|---|---|
| audit-seam | 1.00 | 4.00 | 1/1 | 0/1 | 776/223 | 2443 | 6433 |
| job-store | 1.00 | 1.00 | 1/1 | 1/1 | 1493/171 | 3077 | 2019 |
| notes-console | 4.00 | 2.00 | 0/1 | 1/1 | 5873/950 | 19856 | 5484 |
| notes-http | 1.00 | 4.00 | 1/1 | 0/1 | 5830/491 | 6478 | 7471 |
| payment-gateway | 1.00 | 2.00 | 1/1 | 1/1 | 2109/173 | 3302 | 4069 |
| private-notes-http | 1.00 | 3.00 | 1/1 | 1/1 | 7191/626 | 7705 | 7588 |
| tenant-api | 1.00 | 4.00 | 1/1 | 0/1 | 2103/333 | 3401 | 6832 |
| user-cache | 1.00 | 2.00 | 1/1 | 1/1 | 910/213 | 2597 | 4231 |

`reading` is the proxy token count of what the arm reads (the file, or the pack); `tokens in` is what the endpoint reported, system prompt and retries included.

- pack minus raw, mean rounds per task: +1.38 (the pack LOSES rounds)
- tasks where the pack took more rounds: 6 of 8
- input tokens, pack / raw: 0.90
- verdict: HOLD (loses rounds)

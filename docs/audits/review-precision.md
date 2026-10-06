# Review precision audit

## Copilot: gpt-6-luna failed verdicts

Issue: [mahler#737](https://github.com/mkny13/mahler/issues/737), part of #732.

### Frozen sample

Cutoff: 2026-10-01 00:00 America/New_York (2026-10-01T04:00:00+00:00).
Query time (inclusive upper bound): `2026-10-06T19:45:39.308271+00:00`.
Read-only live ledger query; eligible population: **62 verdicts**.
Stable ordering: ascending unique `runs.id`; sample: Python `random.Random(732).sample(population, 30)`, without replacement.
All 62 eligible rows recorded platform `copilot`. Eligibility uses the recorded model, not a current routing alias.
The earliest fail `review_verdict` event is the verdict time; repeated finalization events count once.

Population run IDs, in stable order:

```json
[10321, 10329, 10339, 10340, 10363, 10382, 10384, 10405, 10408, 10413, 10415, 10423, 10429, 10431, 10448, 10453, 10458, 10463, 10468, 10471, 10474, 10479, 10480, 10494, 10496, 10498, 10500, 10502, 10504, 10508, 10521, 10542, 10549, 10568, 10580, 10586, 10606, 10610, 10662, 10672, 10673, 10675, 10681, 10683, 10685, 10691, 10730, 10747, 10759, 10773, 10775, 10785, 10792, 10821, 10823, 10834, 10841, 10865, 10869, 10872, 10875, 10906]
```

Selected run IDs, in draw order:

```json
[10681, 10405, 10823, 10496, 10675, 10673, 10841, 10453, 10363, 10730, 10384, 10568, 10498, 10413, 10906, 10662, 10382, 10423, 10865, 10480, 10672, 10685, 10494, 10504, 10415, 10321, 10521, 10329, 10610, 10747]
```

| Draw | Run | Project issue | PR | Reviewed SHA | Verdict time (UTC) |
| --- | --- | --- | --- | --- | --- |
| 1 | 10681 | [phish-in#507](https://github.com/mkny13/couch-tour/issues/507) | [514](https://github.com/mkny13/couch-tour/pull/514) | [d447520487e6](https://github.com/mkny13/couch-tour/commit/d447520487e6939125977b7fcd5f084171b765a9) | 2026-10-02T20:27:00.116311+00:00 |
| 2 | 10405 | [mahler#613](https://github.com/mkny13/mahler/issues/613) | [637](https://github.com/mkny13/mahler/pull/637) | [27ee26c4733a](https://github.com/mkny13/mahler/commit/27ee26c4733ab73b336bf712e318456a0d25ae5b) | 2026-10-01T22:10:21.203626+00:00 |
| 3 | 10823 | [phish-in#533](https://github.com/mkny13/couch-tour/issues/533) | [534](https://github.com/mkny13/couch-tour/pull/534) | [68b93077e8ff](https://github.com/mkny13/couch-tour/commit/68b93077e8ffc7ce524e574cdae5e830de2fff89) | 2026-10-06T04:22:04.623536+00:00 |
| 4 | 10496 | [phish-in#369](https://github.com/mkny13/couch-tour/issues/369) | [425](https://github.com/mkny13/couch-tour/pull/425) | [975d629712b1](https://github.com/mkny13/couch-tour/commit/975d629712b1820ce1644a8fc294663c3a6c6b4a) | 2026-10-02T03:27:40.860777+00:00 |
| 5 | 10675 | [phish-in#427](https://github.com/mkny13/couch-tour/issues/427) | [512](https://github.com/mkny13/couch-tour/pull/512) | [b2b5f3d19446](https://github.com/mkny13/couch-tour/commit/b2b5f3d19446d2110e5b8e069bb3b395a8732556) | 2026-10-02T19:54:41.739831+00:00 |
| 6 | 10673 | [phish-in#434](https://github.com/mkny13/couch-tour/issues/434) | [458](https://github.com/mkny13/couch-tour/pull/458) | [bd5001710794](https://github.com/mkny13/couch-tour/commit/bd500171079404b20908780a0bc9e616df30ed7d) | 2026-10-02T19:44:44.096775+00:00 |
| 7 | 10841 | [puppy-growth-chart#9](https://github.com/mkny13/puppy-growth-chart/issues/9) | [13](https://github.com/mkny13/puppy-growth-chart/pull/13) | [b2226c908e75](https://github.com/mkny13/puppy-growth-chart/commit/b2226c908e7539a2eadfdc15af8e2dd46403683d) | 2026-10-06T05:28:02.046128+00:00 |
| 8 | 10453 | [phish-in#435](https://github.com/mkny13/couch-tour/issues/435) | [457](https://github.com/mkny13/couch-tour/pull/457) | [db2e7fc3e80b](https://github.com/mkny13/couch-tour/commit/db2e7fc3e80b38bba720be3805d9e0c886702bf7) | 2026-10-01T23:47:25.554080+00:00 |
| 9 | 10363 | [phish-in#431](https://github.com/mkny13/couch-tour/issues/431) | historical PR pending evidence | [9d870a5066ae](https://github.com/mkny13/couch-tour/commit/9d870a5066ae2ea1cdc1c6028183ba7435d2e510) | 2026-10-01T17:10:04.674005+00:00 |
| 10 | 10730 | [phish-in#351](https://github.com/mkny13/couch-tour/issues/351) | [523](https://github.com/mkny13/couch-tour/pull/523) | [33ea7879b01d](https://github.com/mkny13/couch-tour/commit/33ea7879b01db49f6f3510d122421fb25505a3d9) | 2026-10-03T15:10:12.807038+00:00 |
| 11 | 10384 | [mahler#607](https://github.com/mkny13/mahler/issues/607) | [610](https://github.com/mkny13/mahler/pull/610) | [541fe7169c7e](https://github.com/mkny13/mahler/commit/541fe7169c7ee7b11bd4f945df4a1a5bcb3a077c) | 2026-10-01T18:40:09.849694+00:00 |
| 12 | 10568 | [phish-in#492](https://github.com/mkny13/couch-tour/issues/492) | [503](https://github.com/mkny13/couch-tour/pull/503) | [0f29774c18a5](https://github.com/mkny13/couch-tour/commit/0f29774c18a51e560e215b3d3196375253a0e6fb) | 2026-10-02T11:55:55.891966+00:00 |
| 13 | 10498 | [phish-in#369](https://github.com/mkny13/couch-tour/issues/369) | [425](https://github.com/mkny13/couch-tour/pull/425) | [e90032fb1fad](https://github.com/mkny13/couch-tour/commit/e90032fb1fada83c29889b05bd6c19bb6e58da7e) | 2026-10-02T03:53:58.608297+00:00 |
| 14 | 10413 | [hockey#70](https://github.com/mkny13/hockey-draft-copilot/issues/70) | [74](https://github.com/mkny13/hockey-draft-copilot/pull/74) | [78961aed9a1d](https://github.com/mkny13/hockey-draft-copilot/commit/78961aed9a1deb99c765d4a046423d4f548190df) | 2026-10-01T22:22:51.010317+00:00 |
| 15 | 10906 | [mahler#734](https://github.com/mkny13/mahler/issues/734) | [764](https://github.com/mkny13/mahler/pull/764) | [12dc0196723c](https://github.com/mkny13/mahler/commit/12dc0196723c7fd6b1fbc19c85fc1278c6c2ca0c) | 2026-10-06T18:56:09.947513+00:00 |
| 16 | 10662 | [phish-in#369](https://github.com/mkny13/couch-tour/issues/369) | [425](https://github.com/mkny13/couch-tour/pull/425) | [7e6877784f8f](https://github.com/mkny13/couch-tour/commit/7e6877784f8f2a5807df644a13b8dba2f0405eaf) | 2026-10-02T19:23:18.121107+00:00 |
| 17 | 10382 | [phish-in#386](https://github.com/mkny13/couch-tour/issues/386) | [450](https://github.com/mkny13/couch-tour/pull/450) | [0612abfada00](https://github.com/mkny13/couch-tour/commit/0612abfada001703375ba4c12dc1001c1bea84b8) | 2026-10-01T18:28:23.521286+00:00 |
| 18 | 10423 | [mahler#597](https://github.com/mkny13/mahler/issues/597) | [602](https://github.com/mkny13/mahler/pull/602) | [ee32873c1ace](https://github.com/mkny13/mahler/commit/ee32873c1ace3c6787f3dbb3f10ac74c742c2f8d) | 2026-10-01T22:43:59.325405+00:00 |
| 19 | 10865 | [mahler#714](https://github.com/mkny13/mahler/issues/714) | [731](https://github.com/mkny13/mahler/pull/731) | [7770149f4f8c](https://github.com/mkny13/mahler/commit/7770149f4f8c9de783c088169068c18ac04554d2) | 2026-10-06T12:43:42.100696+00:00 |
| 20 | 10480 | [mahler#643](https://github.com/mkny13/mahler/issues/643) | [648](https://github.com/mkny13/mahler/pull/648) | [0d2b0bb7c8f9](https://github.com/mkny13/mahler/commit/0d2b0bb7c8f922d37b6b6d12fe790c669fdf2875) | 2026-10-02T00:49:41.580852+00:00 |
| 21 | 10672 | [phish-in#369](https://github.com/mkny13/couch-tour/issues/369) | [425](https://github.com/mkny13/couch-tour/pull/425) | [1b44b06fa79d](https://github.com/mkny13/couch-tour/commit/1b44b06fa79de95f72f977f2b841245ae697ba7e) | 2026-10-02T19:42:15.653258+00:00 |
| 22 | 10685 | [phish-in#507](https://github.com/mkny13/couch-tour/issues/507) | [514](https://github.com/mkny13/couch-tour/pull/514) | [148ac5cdaa3e](https://github.com/mkny13/couch-tour/commit/148ac5cdaa3eea7ab4911e4e1ebbf03e3d348427) | 2026-10-02T20:42:05.775551+00:00 |
| 23 | 10494 | [phish-in#369](https://github.com/mkny13/couch-tour/issues/369) | [425](https://github.com/mkny13/couch-tour/pull/425) | [cdd788c39207](https://github.com/mkny13/couch-tour/commit/cdd788c3920780fbbefb5a42e496cb1fbfc7623f) | 2026-10-02T03:19:36.285395+00:00 |
| 24 | 10504 | [phish-in#428](https://github.com/mkny13/couch-tour/issues/428) | [460](https://github.com/mkny13/couch-tour/pull/460) | [9782a271feae](https://github.com/mkny13/couch-tour/commit/9782a271feaea1d9797943afa4dae7b64c2354f8) | 2026-10-02T04:30:48.304174+00:00 |
| 25 | 10415 | [mahler#623](https://github.com/mkny13/mahler/issues/623) | [638](https://github.com/mkny13/mahler/pull/638) | [f84db9867394](https://github.com/mkny13/mahler/commit/f84db98673942e3591062e74804eb520c4c5f6c7) | 2026-10-01T22:28:01.957006+00:00 |
| 26 | 10321 | [mahler#607](https://github.com/mkny13/mahler/issues/607) | [610](https://github.com/mkny13/mahler/pull/610) | [1d34dd3fa7d9](https://github.com/mkny13/mahler/commit/1d34dd3fa7d972b60546884792cc3dbd36eb468d) | 2026-10-01T15:48:12.187141+00:00 |
| 27 | 10521 | [phish-in#444](https://github.com/mkny13/couch-tour/issues/444) | [469](https://github.com/mkny13/couch-tour/pull/469) | [e18fcc241061](https://github.com/mkny13/couch-tour/commit/e18fcc241061f933228f58f1d390ec1bdf6ed58f) | 2026-10-02T05:41:15.661120+00:00 |
| 28 | 10329 | [mahler#607](https://github.com/mkny13/mahler/issues/607) | [610](https://github.com/mkny13/mahler/pull/610) | [e42035e3cb38](https://github.com/mkny13/mahler/commit/e42035e3cb38127cb5f1d8022eb2c6a50b55fb90) | 2026-10-01T15:57:49.873670+00:00 |
| 29 | 10610 | [phish-in#428](https://github.com/mkny13/couch-tour/issues/428) | [460](https://github.com/mkny13/couch-tour/pull/460) | [145a4d8053e9](https://github.com/mkny13/couch-tour/commit/145a4d8053e9ddc0e23a92a16617d724d23b2791) | 2026-10-02T18:03:19.487795+00:00 |
| 30 | 10747 | [phish-in#522](https://github.com/mkny13/couch-tour/issues/522) | [529](https://github.com/mkny13/couch-tour/pull/529) | [a6608fe315d0](https://github.com/mkny13/couch-tour/commit/a6608fe315d0a2f290914156c4347f65ae59a8ed) | 2026-10-03T16:08:30.942770+00:00 |

### Adjudication

Evidence review in progress; no precision result claimed at this checkpoint.

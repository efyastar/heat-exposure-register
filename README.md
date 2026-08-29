# Heat Exposure Register

**A shift-planning tool for outdoor work crews in Phoenix, built on street-level
temperature data.**

FortyGuard Hackathon'26 · Track 1 — Resilient Cities & Infrastructure

**Live demo:** _add your deployed URL here_
**Video:** _add your video link here_

---

## The problem

Phoenix kills outdoor workers. Construction, landscaping, roofing and delivery
crews work through summers where afternoon air routinely passes 42 °C (108 °F),
and the tool most foremen use to decide whether to send a crew out is a
city-wide weather app reporting a temperature measured at the airport.

That number is the same for every job site in the metro. This tool starts from
the observation that it should not be.

## What it does

A foreman opens one page and sees, in this order:

1. **A verdict for today** — *"Work until noon"*, *"Stop outdoor work now"*, or
   *"Safe to work today"* — in plain language, in Fahrenheit, above the fold, with
   no chart to interpret.
2. **The hour-by-hour shape of the day** as a green/red strip, readable at arm's
   length.
3. **Their sites ranked worst-first** by how many hours a day each one spends
   above the danger threshold, with the cost that exposure implies.
4. **Where the heat actually sits** across the city, tile by tile.

Everything below that — the forecast chart, the day profile, the cost model, the
method — is for whoever wants to check the working.

## The finding that matters

Across the study area, ground-level exposure above 40 °C (104 °F) ranges from
**3.6 to 6.1 hours per day**. Two crews working the same city, on the same days,
differ by **two and a half hours of dangerous heat daily** — 77 hours across a
31-day summer window — purely by where they are standing.

Nobody can act on that today, because at city-wide resolution the difference
does not exist.

---

## What I tested and rejected

**The original plan was a cooling simulator**: move a tree-canopy slider, predict
the temperature drop, cost the intervention. It is one of the example projects
in the track brief, and it demos well.

I tested whether it was defensible before building it. It was not.

I joined **7,347 tiles** of measured temperature to land cover from
OpenStreetMap — 38,649 building footprints, 41,464 road segments, 810 green
spaces, 9,906 mapped trees — and trained gradient-boosted regressors under
**spatial block cross-validation**, partitioning the city into 36 blocks and
holding out whole blocks. Neighbouring tiles are near-identical, so a random
split would let a model memorise a neighbourhood and then be scored on its
next-door tile; that number would have looked excellent and meant nothing.

| Model | R² (block-held-out) | MAE |
|---|---|---|
| Predict the mean everywhere | 0.000 | 0.514 h |
| Coordinates only (lat/lon) | **0.846** | 0.085 h |
| Coordinates + all land cover | 0.836 | 0.089 h |

**Urban form contributed −0.009 R².** It made the model slightly worse.
Permutation importance: `lat` +1.95, `lon` +0.45, and every land-cover feature at
+0.0001 or below. `building_frac` — with excellent variance, 0 to 0.96, from
38,649 mapped buildings — scored **zero**.

Suspecting that daytime heat masks the effect (cities are most thermally uniform
at peak sun, and the urban heat island is a nocturnal phenomenon), I repeated
the whole test against **pre-dawn temperature**, where the effect should be
strongest. Same answer: **−0.004**.

At this resolution the data encodes *where* heat is, extremely well, but not
*why*. A canopy slider would have moved a number the data cannot justify.

So the tool reports exposure that was measured, and makes no causal claims. The
negative result is in the product, not hidden behind it — the method section on
the page states it in full.

Reproduce it: `python3 build_features.py && python3 train_model.py`.

---

## What I found in the API

Things I found by probing rather than reading the docs. Each one changed the build.

**Study-area size decides whether there is any signal at all.** A 2 km box in
central Phoenix showed 0.08 °C of spread across 7,072 tiles — nothing to model
and nothing to rank. The same day, over a transect running from downtown to South
Mountain, spread was 0.94 °C. Twelve times more. Choose an area that crosses real
land-use boundaries or the data will look flat.

**Snapshots barely discriminate; duration does.** Peak temperature separates sites
by under 1 °C. Hours-above-threshold separates them by 77 hours. The register is
built on `analytic_type="exceedance"` for that reason, and it is also the more
actionable metric — *"two and a half more dangerous hours a day"* beats *"0.4 °C
warmer"* for anyone making a decision.

**Threshold choice is narrow.** At 38 °C every tile returned the full 24 hours
(saturated). At 43 °C tiles returned **negative hours** — interpolation past the
edge of the data. 40 °C sits inside the reliable band and happens to be a
meaningful safety line (104 °F).

**The forecast window is shorter than documented, and it shrinks.** Advertised as
12 hours ahead. Probed at 07:31 it reached 14:00; probed again at 10:00 it also
reached 14:00. It is a **fixed afternoon cutoff, not a rolling horizon** — by
mid-afternoon there is no forecast left at all.

**Beyond that cutoff the API returns empty rather than erroring.** The request
succeeds, status reads `completed`, `stats_data` comes back with `activity_id` and
`n_cells` — and zero tiles. Hackathon support confirmed this is intended: the
request is valid, there is simply no data for that time, so it is a "no data here"
response rather than a failure. It is still easy to misread — code that does not
check `map_data.features.length` treats it as *"no heat anywhere."* Every call in
this project checks tile count before using a response.

**Forecast layers are spatially flat.** Spread collapses with horizon: 0.40 °C at
dawn, 0.11 °C at +3 h, 0.025 °C at +6 h. Forecasts can say how dangerous the next
few hours are; they cannot say which site is worse. Site ranking therefore uses
only historical layers — a deliberate split, not an oversight.

**The docs contradict themselves on units.** The quickstart client's docstring says
heatmap tiles are Fahrenheit; the repository README says Celsius exclusively.
Empirically it is **Celsius** — Phoenix in August returned ~42, not ~108 — and
support confirmed the docstring is the error. Worth flagging because `threshold` is
documented in °C, so reading tiles as °F would corrupt every exceedance result
without raising anything.

**`env_params` heat index is not a diurnal curve.** It holds temperature fixed and
varies only humidity, so it peaks around 2 a.m. and produced a 159 °F reading at
05:00 in FortyGuard's own sample. Unusable as a model feature; excluded.

---

## Cost model, and its honest limits

Cost per case is well documented. Incidence per hour of heat exposure is not.

- **$757** per heat-related emergency-department visit
- **$14,900** per heat-related hospital admission

(HCUP 2020, via the Center for American Progress.)

The dose-response link — how many extra illness cases result from X extra hours
above a threshold — is **not established in the literature**. I looked. So the
tool does not pretend to know it: incidence is a slider the user sets, defaulted
and labelled as an assumption.

What survives that uncertainty is the **comparison**. One assumption applies to
every site, so the ranking and the ratios between sites hold regardless of where
the slider sits. Drag it in the app and watch the absolute dollars move while the
order never changes. That is the output I stand behind; the level is the user's
input.

---

## Data

| Source | Used for | Access |
|---|---|---|
| FortyGuard Temperature API | 2 m ambient air temperature, street resolution | Hackathon key |
| OpenStreetMap (via osmnx) | Land cover, for the model I rejected | Open, ODbL |
| HCUP 2020 via CAP | Cost per heat illness case | Public |

Study area: ~62 km² of Phoenix, roughly 13 km north–south by 5 km across, at 100 m
tiles (6,137 of them). Exposure window 2026-07-20 to 2026-08-19. Hourly profile
from 2026-08-03.

The thirteen demo sites are **real public locations**, picked off the measured grid
so they span the city's full exposure range rather than clustering. They stand in
for a contractor's job list; the method works for any US coordinates.

---

## Running it

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add FORTYGUARD_API_KEY
```

Build the data (~30 minutes, ~150k credits; resumable — rerun to continue):

```bash
python3 build_site_data.py     # site exposure, hourly profile, forecast
python3 build_grid.py          # full tile grid for the map
python3 prepare_web_data.py    # compact everything into web/data.json
```

Serve it:

```bash
cd web && python3 -m http.server 8000
```

Reproduce the rejected model:

```bash
python3 build_features.py      # joins tiles to OpenStreetMap land cover
python3 train_model.py         # spatial block CV, prints the comparison table
```

### Files

```
build_site_data.py    per-site exposure, hourly profile, forecast (resumable)
build_grid.py         full tile grid with geometry, for the map
prepare_web_data.py   2.1 MB of polygons -> ~160 KB web/data.json
build_features.py     tile -> OpenStreetMap land-cover features
train_model.py        spatial block cross-validation, the negative result
web/index.html        the tool (static, no runtime API calls)
web/data.json         precomputed data
```

## Design decisions

**Static site, no runtime API calls.** Calls take 30 s to 2 minutes; that is
unusable in a live demo and would put an API key in a public deployment. All data
is precomputed. The page loads instantly and cannot fail because a request timed
out while someone is watching.

**Fahrenheit, not Celsius.** The audience is a Phoenix job site. 40 °C means
nothing there; 104 °F means everything. Celsius appears in brackets where
precision matters.

**Hours per day, not hours per window.** 5.9 is a number a person feels. 182 is
not.

**Rank-based risk tiers.** Sites cluster near the top of the range, so splitting
on normalised value labelled nine of thirteen "worst" and told the user nothing.
Terciles by rank are honest about relative position.

## Credits

The `fortyguard/` package is FortyGuard's own API client, vendored unchanged from
their [Temperature API Quickstart](https://github.com/FortyGuard-Tech/temperature-api-quickstart)
so this repository runs standalone. Everything else — the data pipeline, the
model and its evaluation, the cost layer and the interface — is my own work.

## Limitations

- One city, one summer window. The method generalises to any US coordinates;
  these findings do not.
- Site ranking uses a single 31-day window. A different summer could reorder
  sites, though the north–south structure is unlikely to invert.
- The forecast is only as good as FortyGuard's, and it stops each afternoon.
- Absolute costs are illustrative. Relative costs are the claim.
- Demo sites stand in for a real client's job list.

# Snow season windows by mountain range

Used by `src/weather.py` (`_SEASON_WINDOWS`) to determine when to show seasonal
snowfall accumulation alongside the always-on recent 15-day signal.

## Two snowfall signals

| Signal | Window | Shown when |
|---|---|---|
| **Recent snowfall** | Past 15 days, events >15 cm/day flagged | Always, all ranges |
| **Seasonal accumulation** | Since season start to today | Only when in-season for the range |

## Northern Hemisphere ranges

| Range | Season start | Season end | Driver |
|---|---|---|---|
| Alps | Nov 1 | May 31 | Westerlies; Nov–Apr is the de facto standard per SLF/Météo-France/EAWS |
| Pyrenees | Nov 1 | May 31 | Same as Alps; Météo-France bulletins run mid-Dec through end-Apr |
| Caucasus | Oct 1 | May 31 | Mid-latitude westerlies + Arctic air; slightly earlier than Alps |
| Scandinavia | Oct 1 | Apr 30 | Lasting snow at altitude from late September/October |
| Japan (Japanese Alps) | Dec 1 | Apr 30 | Winter monsoon off Sea of Japan; heavy but starts December, not November |
| Tian Shan | Oct 1 | Apr 30 | Continental climate, similar timing to Caucasus |
| Altai | Nov 1 | Apr 30 | Siberian continental; first high-altitude snows late September |
| Karakoram | Nov 1 | Apr 30 | Winter westerlies (Dec–Mar peak) — opposite of Himalaya proper |
| Hindu Kush | Nov 1 | Apr 30 | Similar to Karakoram |
| Alaska | Sep 1 | May 31 | Year-round snowpack; meaningful accumulation starts September |
| Pacific NW (Cascades) | Nov 15 | Apr 30 | Heavy maritime snowpack; NWAC operations start mid-November |
| Sierra Nevada | Nov 1 | Apr 30 | Mediterranean; April 1 is the reference peak for snowpack measurement |
| Colorado Rockies | Oct 1 | May 31 | Continental; October faceting events matter for snowpack structure |
| Appalachians | Nov 1 | Mar 31 | Alpine conditions thin and unreliable; Mt Washington etc. |

## Southern Hemisphere ranges

| Range | Season start | Season end | Notes |
|---|---|---|---|
| Patagonia (<39°S) | May 1 | Oct 31 | SH winter dominant; similar to New Zealand |
| Central Andes (~20–35°S) | Apr 1 | Sep 30 | SH winter = wet/accumulation season; summer (Dec–Feb) = dry, best climbing |
| New Zealand | May 1 | Oct 31 | Ski fields open mid-June; snow from May at altitude |

## Ranges with no seasonal summary

| Range | Reason |
|---|---|
| **Himalaya** | Monsoon-accumulation range: ~75% of annual snowfall falls during the SW monsoon (June–Sep), not in winter. Seasonal totals are misleading. The code outputs a fixed note directing users to local sources. |
| Northern Andes (<15°S) | Equatorial/tropical; year-round snow at altitude, no distinct season |
| Mexico (volcanic: Orizaba, Iztaccíhuatl, Popocatépetl) | Year-round permanent ice on rapidly shrinking glaciers; best climbing Oct–Mar but no seasonal cycle |
| Vosges / low-altitude ranges | Low altitude, unreliable snow; not a meaningful alpine season |
| Unknown | Fall-through for unclassified coords |

## Notes on implementation

- Alps and Pyrenees are classified by **point-in-polygon** against `liste-massifs.geojson`
  (French massifs only: Alpes du Nord, Alpes du Sud, Pyrenees, Corse). Swiss, Austrian,
  and Italian Alpine routes fall through to the bbox table — these are broadly similar
  in season timing to the French Alps and will typically classify as "unknown" unless
  caught by the Alps bbox (which is not included — the GeoJSON is the authoritative source).
  **TODO:** add a broader Alps bbox as a catch-all for non-French Alpine coords.
- Karakoram and Himalaya bboxes overlap; Karakoram is listed first in the bbox table,
  so Karakoram coords are caught before Himalaya.
- The French massif GeoJSON only covers French territory. Swiss/Austrian/Italian Alpine
  routes not caught by the polygon will return "unknown" and show recent-only snowfall.
  This is conservative and correct.

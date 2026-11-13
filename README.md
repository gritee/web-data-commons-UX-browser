# Web Data Commons subset browser

Browse the [Web Data Commons](https://webdatacommons.org/structureddata/) schema.org class-specific subsets for every release, list each subset's files, and convert any `.gz` N-Quads file to CSV or JSON on your machine.

## Background

[schema.org](https://schema.org/) is a shared vocabulary, started in 2011 by Google, Microsoft, Yahoo and Yandex, that websites use to describe what a page is about in a machine-readable way: a `Recipe` with its ingredients and cook time, a `Product` with its price, an `Event` with its date. Sites embed these descriptions in their HTML as JSON-LD, Microdata or RDFa, mainly so search engines can show rich results.

[Web Data Commons](https://webdatacommons.org/) (WDC) is a project of the Data and Web Science Group at the University of Mannheim. It extracts that embedded structured data from the [Common Crawl](https://commoncrawl.org/), a public crawl of billions of web pages, and publishes the result for anyone to download. A new extraction is released roughly once a year.

Because the full extraction is far too large for most uses, WDC also publishes **class-specific subsets**: for each of about 50 common schema.org classes, the data from every page that contains at least one entity of that class. These are what this tool browses. Each subset is a set of gzipped [N-Quads](https://www.w3.org/TR/n-quads/) files, where every line is one statement (subject, property, value) plus the URL of the page it came from. That format is precise but awkward to work with directly, which is why the tool converts it to CSV or JSON.

## Requirements

- Python 3.8 or newer (no extra packages)
- A modern browser
- Internet access to `webdatacommons.org` and `data.dws.informatik.uni-mannheim.de`

## Start the server

From this folder:

```sh
python3 wdc_browser.py
```

This starts a local server and opens `http://127.0.0.1:8765/` in your default browser. Leave the terminal open while you use the page; press `Ctrl+C` to stop.

| Option | Effect |
| --- | --- |
| `--port 9000` | Use another port. If the port is busy, the next free one (up to 19 higher) is used; the terminal prints the actual address. |
| `--no-browser` | Don't open a browser window; open the printed address yourself. |

Opening `wdc_browser.html` directly from disk does not work: the page needs the server to fetch the WDC pages and to download and convert files. It will show the command above instead.

## Using the page

### 1. Pick a release

The pills at the top list every release that has a subsets page (2013-11 to 2024-12 at the time of writing). The newest is selected on first load. The `↻` pill re-checks the WDC site for new releases.

### 2. Find a subset

The table lists each schema.org class subset with its quads, URLs, hosts, size and most frequent classes.

- Type in **Filter subsets…** to narrow by name.
- Click a column heading to sort; click again to reverse.
- The subset name links to its schema.org definition; **Original page ↗** opens the WDC page the table was read from.
- `sample`, `lookup file` and `pld stats file` open those files in a new tab.

### 3. Open the subset's files

- **Browse files** lists the subset's folder. Each `.gz` quads file has a **Convert…** button; `raw .gz` is a plain browser download of the untouched file.
- Releases up to 2020 publish one `.gz` per subset, so the button appears in the subsets table itself as **Convert schema_Recipe.gz…**. The 2018–2020 releases list each class twice, tagged *Microdata* and *JSON-LD*.
- **← subsets** (or the browser's back button) returns to the table. Every view has its own URL, so views can be bookmarked.

### 4. Convert a file

**Convert…** opens a dialog:

| Field | Meaning |
| --- | --- |
| Entity type | The schema.org class to export, one output item per entity. Pre-filled with the subset's class. Leave empty to export every top-level entity on each page. |
| Maximum items | Stop after this many items. `0` converts the whole file. |
| Output format | **CSV**: one flattened row per item. **JSON**: an array of nested objects. |
| Output file path | Defaults to your Downloads folder, e.g. `~/Downloads/wdc_2024-12_Recipe_part_0.csv`. A folder path also works; the default file name is used inside it. You are asked before an existing file is overwritten. |
| Add full `json` column to CSV | Appends each item's complete nested JSON as a last column. |
| Keep the downloaded .gz | Saves the source file next to the output as `<name>.nq.gz`. A later conversion using the same output name reuses it instead of downloading again. |
| Keep the extracted .nq | Saves the uncompressed quads as `<name>.nq`. |

Press **Start**. Download, extraction and conversion run as one stream, so with an item limit the download stops as soon as enough items are found — a few seconds for 1,000 items. Ticking either *Keep* option processes the whole file regardless of the limit; files are typically 100–300 MB compressed and around ten times that extracted.

### 5. Follow progress

The **Conversions** panel in the bottom-right corner shows each job's phase, item / page / quad counts and bytes read.

- **Cancel** stops a job and removes its partial files.
- **Show in folder** reveals a finished file in Finder / Explorer.
- Jobs run one at a time, because the data server rejects parallel downloads; extra jobs wait as *Queued*.
- The job list is kept only while the server is running.

## Output format

**CSV**

- First columns: `page_url`, then `@id`, `@type`, `name` when present.
- Remaining properties are ordered by how often they occur, limited to the 250 most common.
- Nested entities produce a column with their best text (`author`) plus one column per sub-property (`aggregateRating.ratingValue`).
- Repeated values are joined with ` | `.

**JSON**

- One array; each element is `{"page_url": …, "@type": …, <properties>}` with nested entities kept as objects.

In both formats, an entity that appears twice on the same page under the same name (commonly once as Microdata and once as JSON-LD) is written once.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "The local server is not responding" | The server was stopped. Run `python3 wdc_browser.py` again and reload. |
| "bad token - reload the page" | The server was restarted. Reload the page. |
| "Could not reach the Web Data Commons server" with a certificate error | On python.org builds for macOS, run `Install Certificates.command` from the Python application folder, or `pip install certifi`. |
| "Server is rate limiting - retrying" | The data server is throttling; the job retries by itself up to three times. |
| A job finishes with 0 items | The file holds no entities of that type. Try another entity type or leave the field empty. |

## Files

| File | Purpose |
| --- | --- |
| `wdc_browser.py` | Local server: reads the WDC pages, downloads, extracts and converts. |
| `wdc_browser.html` | The page served by it. |

The server listens on `127.0.0.1` only and fetches only from the two WDC hosts.

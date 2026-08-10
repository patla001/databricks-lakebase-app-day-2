# Screenshots

The root `README.md` links to two images from this directory. Drop them in with
exactly these filenames and they will render — no markdown changes needed.

| filename | what to capture |
|---|---|
| `watchlist.png` | The app at `/` with a few tickers and their prices in the table. Run `python app.py` and use `http://localhost:8000`, or take it from the deployed app. |
| `search.png` | Search results. There is no search box in the UI yet, so use cell 6c of `notebooks/Lakebase-Console.ipynb` — its `display()` output is a clean table of ticker / title / similarity. A terminal showing the `curl "localhost:8000/search?q=..."` JSON works too. |

Keep them under ~500KB each and crop to the content; a full-screen capture at
retina resolution is mostly browser chrome.

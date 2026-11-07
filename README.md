# SEO Intelligence Platform V4 — GitHub / Streamlit deployment

A free-to-run Streamlit app for screening Google Search Console query and page data, flagging possible SEO opportunities, and exporting a structured Excel action plan. It uses transparent rules, not a paid AI API.

## Deploy in your browser (no Terminal)

1. Download and **extract** `SEO_Intelligence_V4_GitHub_Ready.zip`.
2. Sign in to https://github.com/ and create a new repository called `seo-intelligence-platform` (Public for a portfolio demo). You can leave the initial repository empty.
3. In that repository, choose **Add file > Upload files**. Upload the **files inside the extracted folder**, not the ZIP itself:
   - `app.py`
   - `seo_engine.py`
   - `requirements.txt`
   - `fictional_GSC_demo.zip`
   - `README.md`
   - `.gitignore` (optional; hidden on some computers)
4. Commit changes. Check that `app.py` and `requirements.txt` show in the root of the GitHub repository (not inside an extra folder).
5. Sign in to https://share.streamlit.io/ and connect your GitHub account.
6. Choose **Create app > Yup, I have an app**, and select:
   - Repository: `<your-github-username>/seo-intelligence-platform`
   - Branch: `main`
   - Main file path: `app.py`
   - App URL/subdomain: choose an available name such as `seo-intelligence-v4`.
7. Click **Deploy**. Streamlit Community Cloud will provide a shareable `https://...streamlit.app` URL. The URL only exists after you complete deployment.
8. Try **Explore fictional demo instead** before uploading any authorized GSC export.

## Important privacy and limitations

- **Never commit or publish real client GSC exports**, confidential files, passwords, or API keys to GitHub. This package contains **only fictional data**.
- A public app is accessible to anyone who knows or discovers its URL. Do **not** upload confidential client data to a public deployment without explicit authorization and appropriate safeguards. Use a private app or trusted hosting if required.
- Streamlit Community Cloud may put an unused app to sleep; visitors can wake it. It is a fixed shareable URL, not guaranteed 24/7 uptime.
- The ordinary GSC ZIP contains separate Queries and Pages tables. Accurate keyword-to-page matching requires a combined Query + Page CSV or an authorized GSC API export.
- The tool flags candidate SEO opportunities, not guaranteed problems or traffic gains. Validate relevance, intent, true ranking page, and conversions before making recommendations.

## Troubleshooting

- If GitHub displays only the ZIP, **extract it and upload the individual files**.
- If Streamlit says `No module named seo_engine`, ensure `seo_engine.py` sits next to `app.py` at repository root.
- If it says `File does not exist: app.py`, choose `app.py` in the **Main file path** field.
- If a demo cannot load, check that `fictional_GSC_demo.zip` is in the root of the repo.
- To review deployment errors, use **Manage app > Logs** on Streamlit Community Cloud.

## Run locally (optional)

```bash
python -m pip install -r requirements.txt
streamlit run app.py
```

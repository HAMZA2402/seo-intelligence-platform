"""SEO Opportunity Analyzer V4 - self-contained Google Colab analysis engine.
Uses only standard GSC exports and optional authorized Search Console API.
"""
import io
import json
import math
import os
import re
import html as html_lib
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# Clients edit configuration in the notebook SETTINGS cell, not inside this code.

GSC_COLUMNS = {
    'top queries': 'Query', 'query': 'Query', 'queries': 'Query', 'search query': 'Query',
    'top pages': 'Page', 'page': 'Page', 'pages': 'Page', 'landing page': 'Page', 'url': 'Page',
    'clicks': 'Clicks', 'impressions': 'Impressions', 'ctr': 'CTR',
    'ctr (%)': 'CTR', 'position': 'Position', 'date': 'Date',
}


def _csv_from_bytes(data):
    errors = []
    for enc in ('utf-8-sig', 'utf-16', 'cp1252'):
        try:
            df = pd.read_csv(io.BytesIO(data), encoding=enc, sep=None, engine='python',
                             on_bad_lines='error')
            if len(df.columns) >= 2:
                df.columns = [str(x).strip() for x in df.columns]
                return df
        except Exception as exc:
            errors.append(str(exc))
    raise ValueError('Unable to read CSV. Use an unmodified Google Search Console CSV export. Details: ' + errors[-1][:150])


def _normalize_frame(df):
    result = df.copy()
    result = result.rename(columns={c: GSC_COLUMNS.get(str(c).strip().lower(), str(c).strip())
                                     for c in result.columns})
    return result


def _number_col(series):
    return pd.to_numeric(series.astype(str).str.replace(',', '', regex=False)
                         .str.replace(' ', '', regex=False), errors='coerce')


def _canonical_metrics(frame, dimension=None, api_ctr_ratio=False):
    result = _normalize_frame(frame)
    required = ['Clicks','Impressions','Position']
    missing = [x for x in required if x not in result.columns]
    if missing: raise ValueError('GSC file missing metrics: ' + ', '.join(missing))
    if dimension is not None and dimension not in result.columns:
        raise ValueError('GSC file missing dimension: ' + dimension)
    result = result.copy()
    for field in ('Clicks','Impressions','Position'):
        result[field] = _number_col(result[field])
    if 'CTR' in result.columns:
        header_is_pct = any(str(c).strip().lower() == 'ctr (%)' for c in frame.columns)
        raw = result['CTR'].astype(str)
        percent_sign = raw.str.contains('%',regex=False).any()
        c = _number_col(raw.str.replace('%','',regex=False))
        if api_ctr_ratio or (not header_is_pct and not percent_sign and c.notna().any() and c.max() <= 1):
            c = 100 * c
        result['CTR (%)'] = c
    else:
        result['CTR (%)'] = 100 * result['Clicks'] / result['Impressions'].replace(0, np.nan)
    result['CTR (%)'] = result['CTR (%)'].fillna(100 * result['Clicks'] / result['Impressions'].replace(0,np.nan))
    subset = required + (['CTR (%)'] if 'CTR (%)' in result else [])
    result = result.dropna(subset=subset)
    result = result[(result.Clicks >= 0) & (result.Impressions > 0) & (result.Position > 0)]
    if dimension:
        result[dimension] = result[dimension].astype('string').str.strip()
        result = result[result[dimension].notna() & result[dimension].str.len().gt(0)]
    for field in ('Clicks','Impressions'):
        result[field] = result[field].round().astype('int64')
    return result.reset_index(drop=True)


def parse_upload(filename, binary):
    """Return dict of frames and metadata. ZIP is preferred: one upload."""
    frames = {'Queries': pd.DataFrame(), 'Pages': pd.DataFrame(),
              'Chart': pd.DataFrame(), 'QueryPage': pd.DataFrame()}
    meta = {'source':filename, 'input_mode':'ZIP/CSV upload', 'date_range':'Not specified in file',
            'search_type':'Not specified in file', 'mapping':'Not available (ZIP dimensions are separate)'}
    if filename.lower().endswith('.zip'):
        try:
            zf = zipfile.ZipFile(io.BytesIO(binary))
        except zipfile.BadZipFile as exc:
            raise ValueError('The ZIP file could not be opened. Download the original GSC Performance ZIP.') from exc
        with zf:
            files = [i for i in zf.infolist() if not i.is_dir() and
                     i.filename.lower().endswith('.csv') and not i.filename.startswith('__MACOSX')]
            if not files: raise ValueError('ZIP has no CSV files.')
            if len(files) > 25: raise ValueError('Unexpectedly large number of CSV files in ZIP.')
            if sum(i.file_size for i in files) > 60_000_000:
                raise ValueError('ZIP CSV contents exceed the 60 MB safety limit.')
            for member in files:
                name = os.path.basename(member.filename).strip().lower()
                data = _csv_from_bytes(zf.read(member))
                cols = {str(c).strip().lower() for c in data.columns}
                if ('query' in cols or 'top queries' in cols) and ('page' in cols or 'top pages' in cols):
                    frames['QueryPage'] = data
                elif name.startswith('queries') or ('top queries' in cols):
                    frames['Queries'] = data
                elif name.startswith('pages') or ('top pages' in cols):
                    frames['Pages'] = data
                elif name.startswith('chart') or ('date' in cols and 'clicks' in cols):
                    frames['Chart'] = data
                elif name.startswith('filters') or ('filter' in cols and 'value' in cols):
                    lower = {str(c).strip().lower(): c for c in data.columns}
                    for _, row in data.iterrows():
                        field = str(row[lower.get('filter','Filter')]).lower()
                        val = str(row[lower.get('value','Value')]).strip()
                        if 'date' in field: meta['date_range'] = val
                        if 'search type' in field: meta['search_type'] = val
    elif filename.lower().endswith('.csv'):
        frame = _csv_from_bytes(binary)
        cols = {str(c).strip().lower() for c in frame.columns}
        if ('query' in cols or 'top queries' in cols) and ('page' in cols or 'top pages' in cols):
            frames['QueryPage'] = frame
        elif 'top queries' in cols or 'query' in cols:
            frames['Queries'] = frame
        elif 'top pages' in cols or 'page' in cols:
            frames['Pages'] = frame
        else:
            raise ValueError('CSV needs a Query or Page column and standard GSC metrics.')
    else:
        raise ValueError('Upload a GSC .zip or .csv file. Do not upload the older Excel report.')
    return frames, meta


def _aggregate_pairs(frame, group):
    if frame.empty:
        return pd.DataFrame(columns=[group,'Clicks','Impressions','CTR (%)','Position'])
    g = frame.copy()
    g['_weighted_position'] = g['Position'] * g['Impressions']
    g = g.groupby(group, dropna=False, as_index=False).agg(
        Clicks=('Clicks','sum'), Impressions=('Impressions','sum'),
        _weighted_position=('_weighted_position','sum'))
    g['Position'] = (g['_weighted_position'] / g['Impressions']).round(3)
    g['CTR (%)'] = (100*g['Clicks']/g['Impressions']).round(3)
    return g.drop(columns=['_weighted_position'])


def normalize_inputs(frames, meta):
    q = pd.DataFrame()
    p = pd.DataFrame()
    qp = pd.DataFrame()
    chart = pd.DataFrame()
    if not frames['Queries'].empty:
        q = _canonical_metrics(frames['Queries'], 'Query')
        q = _aggregate_pairs(q,'Query')
    if not frames['Pages'].empty:
        p = _canonical_metrics(frames['Pages'], 'Page')
        p = _aggregate_pairs(p,'Page')
    if not frames['QueryPage'].empty:
        qp = _canonical_metrics(frames['QueryPage'], api_ctr_ratio=False)
        if 'Query' not in qp.columns or 'Page' not in qp.columns:
            raise ValueError('Query+Page table must contain both Query and Page columns.')
        qp['Query'] = qp['Query'].astype(str).str.strip()
        qp['Page'] = qp['Page'].astype(str).str.strip()
        qp = qp[qp['Query'].ne('') & qp['Page'].ne('')].copy()
        pairs = qp.groupby(['Query','Page'],dropna=False,as_index=False).apply(
            lambda g: pd.Series({
                'Clicks':g['Clicks'].sum(), 'Impressions':g['Impressions'].sum(),
                'Position': (g['Position'] * g['Impressions']).sum()/g['Impressions'].sum(),
            }),include_groups=False)
        if isinstance(pairs.index, pd.MultiIndex): pairs = pairs.reset_index()
        pairs['CTR (%)'] = 100*pairs['Clicks']/pairs['Impressions']
        qp = pairs[['Query','Page','Clicks','Impressions','CTR (%)','Position']].copy()
        if q.empty: q = _aggregate_pairs(qp,'Query')
        if p.empty: p = _aggregate_pairs(qp,'Page')
        meta['mapping']='Verified GSC query+page pairs'
    if not frames['Chart'].empty:
        chart = _canonical_metrics(frames['Chart'])
        if 'Date' in chart.columns:
            chart['Date'] = pd.to_datetime(chart['Date'],errors='coerce')
            chart = chart.dropna(subset=['Date']).sort_values('Date')
            if not chart.empty:
                meta['date_range'] = meta['date_range'] if meta['date_range']!='Not specified in file' else \
                    (chart['Date'].min().date().isoformat() + ' to ' + chart['Date'].max().date().isoformat())
    if q.empty and p.empty:
        raise ValueError('No GSC query or page metrics found in your file.')
    if not q.empty:
        q = q.sort_values('Impressions',ascending=False).reset_index(drop=True)
    if not p.empty:
        p = p.sort_values('Impressions',ascending=False).reset_index(drop=True)
    return q, p, qp, chart, meta


def pull_gsc_api(property_id, start_date=None, end_date=None, max_batches=5):
    """Optional Google-authenticated query+page mode. No private API keys required."""
    if not property_id or 'example' in property_id.lower():
        raise ValueError('Specify your exact GSC property, e.g. sc-domain:avalonlaw.com.')
    from google.colab import auth
    auth.authenticate_user()
    import google.auth
    from googleapiclient.discovery import build
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/webmasters.readonly'])
    service = build('searchconsole', 'v1', credentials=credentials, cache_discovery=False)
    now = datetime.now(ZoneInfo('America/Los_Angeles')).date()
    end_date = end_date or (now-timedelta(days=3)).isoformat()
    start_date = start_date or (date.fromisoformat(end_date)-timedelta(days=89)).isoformat()
    if date.fromisoformat(start_date)>date.fromisoformat(end_date):
        raise ValueError('START_DATE must be before END_DATE.')
    def query_dimensions(dimensions):
        rows=[]
        limit=25000
        for batch in range(max_batches):
            body = {'startDate':start_date, 'endDate':end_date, 'dimensions':dimensions,
                    'type':'web','rowLimit':limit,'startRow':batch*limit, 'dataState':'final'}
            resp = service.searchanalytics().query(siteUrl=property_id,body=body).execute()
            r = resp.get('rows',[])
            for item in r:
                record={col:item['keys'][idx] for idx,col in enumerate(dimensions)}
                record.update({'Clicks':item.get('clicks',0), 'Impressions':item.get('impressions',0),
                               'CTR (%)':100*item.get('ctr',0), 'Position':item.get('position',0)})
                rows.append(record)
            if len(r)<limit: break
        return pd.DataFrame(rows)
    raw = {'QueryPage':query_dimensions(['query','page']),
           'Queries':query_dimensions(['query']), 'Pages':query_dimensions(['page']),
           'Chart':query_dimensions(['date'])}
    for kind in raw:
        raw[kind] = _normalize_frame(raw[kind])
    meta = {'source':'GSC Search Analytics API: '+property_id,
            'input_mode':'Google-authenticated GSC API',
            'date_range':start_date+' to '+end_date,
            'search_type':'Web',
            'mapping':'Verified GSC query+page pairs'}
    q,p,qp,chart,meta = normalize_inputs(raw, meta)
    if qp.empty:
        meta['mapping']='API returned no query+page rows; exact mapping unavailable'
    return q,p,qp,chart,meta


def _contains_any(text, terms):
    return any(term.lower() in text.lower() for term in terms if str(term).strip())


def _theme(q, themes):
    lower = str(q).lower()
    for name, terms in themes.items():
        if _contains_any(lower,terms): return name
    return 'Other / validate fit'


def _query_reviews(q, brand_terms, geographic_ambiguity_terms, service_mismatch_terms):
    s = str(q).strip().lower()
    reasons=[]
    serious=False
    if _contains_any(s, geographic_ambiguity_terms):
        reasons.append('Ambiguous geography: location vs firm brand'); serious=True
    if _contains_any(s, service_mismatch_terms):
        reasons.append('Possible service mismatch'); serious=True
    if (len(s)<=5 and not _contains_any(s,brand_terms)):
        reasons.append('Very short / ambiguous query'); serious=True
    if re.search(r'\b(abogado|abogada|abogados|abogadas|mordedura|lesiones|accidentes|responsabilidad)\b',s):
        reasons.append('Spanish-language intent: check language-specific page')
    if len(s.split()) <= 2 and not _contains_any(s,brand_terms):
        reasons.append('Broad intent: verify geography and business relevance')
    return ' | '.join(reasons) if reasons else 'Manual SERP and landing-page validation', serious


def _suggest_action(flags, mapped, is_brand=False):
    if is_brand:
        return 'Check branded SERP features and reputation; avoid creating a duplicate service page.'
    if 'Visibility gap (20+)' in flags:
        return 'Review relevance and SERP intent; inspect existing page coverage and internal links before planning a content update.'
    if 'Low CTR at top-10 position' in flags:
        return 'Review query intent, title/snippet and SERP features; optimize only after confirming click opportunity.'
    if 'Striking distance (8-20)' in flags:
        return 'Review page content depth, internal linking and search intent; test focused improvements.'
    if 'Zero clicks in top-20' in flags:
        return 'Check SERP/intent and snippet competition; zero clicks alone is not proof of an issue.'
    return 'Validate demand, business fit and ranking landing page before making a change.'


def _add_query_page_mapping(q, qp):
    q=q.copy()
    q['Top Ranking Page'] = 'Unmapped - needs GSC query+page data'
    q['Page Match Source'] = 'Not available from separate ZIP query/page tables'
    q['Top Page Impressions'] = np.nan
    q['Top Page Share (%)'] = np.nan
    q['Multi-page Signal'] = ''
    if qp.empty: return q
    pairs=qp.copy()
    pairs['Clicks']=pd.to_numeric(pairs['Clicks'])
    pairs['Impressions']=pd.to_numeric(pairs['Impressions'])
    pairs=pairs.sort_values(['Query','Impressions','Clicks'],ascending=[True,False,False])
    pair_totals=pairs.groupby('Query')['Impressions'].sum().to_dict()
    top=pairs.drop_duplicates('Query',keep='first')[['Query','Page','Impressions']].copy()
    top['Top Page Share (%)'] = [round(100*val/pair_totals[key],1) if pair_totals[key]>0 else np.nan
                                    for key,val in zip(top.Query, top.Impressions)]
    signals={}
    for query,g in pairs.groupby('Query'):
        # Investigation signal only, never proof of cannibalization.
        if len(g)>=2 and g.iloc[1]['Impressions']>=20 and g.iloc[1]['Impressions']/sum(g['Impressions'])>=0.20:
            signals[query]='2+ meaningful ranking URLs: investigate overlap'
    details=top.rename(columns={'Page':'_mapped_url','Impressions':'_mapped_imp'})
    q=q.merge(details,on='Query',how='left')
    match=q['_mapped_url'].notna()
    q.loc[match,'Top Ranking Page'] = q.loc[match,'_mapped_url']
    q.loc[match,'Page Match Source'] = 'GSC query+page data'
    q.loc[match,'Top Page Impressions'] = q.loc[match,'_mapped_imp']
    q.loc[match,'Multi-page Signal'] = q.loc[match,'Query'].map(signals).fillna('')
    q=q.drop(columns=['_mapped_url','_mapped_imp'])
    return q


def analyze_queries(q, qp, cfg):
    if q.empty:
        return pd.DataFrame(),pd.DataFrame()
    out = _add_query_page_mapping(q,qp)
    flags_col=[]; score_col=[]; priority_col=[]; review_col=[]; action_col=[]; theme_col=[]; intent_col=[]
    for _,r in out.iterrows():
        query=str(r.Query)
        imps=int(r.Impressions); clicks=int(r.Clicks)
        pos=float(r.Position); ctr=float(r['CTR (%)'])
        is_brand=_contains_any(query,cfg['BRAND_TERMS'])
        flags=[]; score=0
        if imps>=cfg['MIN_IMPRESSIONS']:
            if 0<pos<=10 and ctr < cfg['LOW_CTR_PERCENT']:
                flags.append('Low CTR at top-10 position'); score+=26
            if 8<=pos<=20:
                flags.append('Striking distance (8-20)'); score+=25
            if clicks==0 and pos<=20:
                flags.append('Zero clicks in top-20'); score+=15
            if pos>20 and imps>=cfg['VISIBILITY_GAP_MIN_IMPRESSIONS']:
                flags.append('Visibility gap (20+)'); score+=28
            if flags:
                score += (12 if imps>=1000 else 8 if imps>=300 else 3)
        review, blocking = _query_reviews(query,cfg['BRAND_TERMS'],
                     cfg['GEO_AMBIGUITY_TERMS'],cfg['SERVICE_MISMATCH_TERMS'])
        if is_brand and flags:
            review='Branded search: validate before applying generic CTR rules | '+review
        if blocking and flags:
            priority='Review'
        elif score>=60:
            priority='High'
        elif score>=38:
            priority='Medium'
        elif flags:
            priority='Low'
        else:
            priority='None'
        mapping = r['Page Match Source']=='GSC query+page data'
        flags_col.append('; '.join(flags))
        score_col.append(int(score))
        priority_col.append(priority)
        review_col.append(review)
        action_col.append(_suggest_action(flags, mapping, is_brand=is_brand) if flags else '')
        theme_col.append(_theme(query,cfg['TOPIC_THEMES']))
        intent_col.append('Brand' if is_brand else
                          ('Informational' if re.search(r'\b(what|how|why|when|can|does|is|are|who)\b',query.lower())
                           else 'Mixed / commercial to check'))
    out['Topic'] = theme_col
    out['Intent'] = intent_col
    out['Opportunity Signals'] = flags_col
    out['Score'] = score_col
    out['Priority'] = priority_col
    out['Review Required'] = review_col
    out['Suggested Next Action'] = action_col
    rank={'High':0,'Medium':1,'Low':2,'Review':3,'None':4}
    out['_priority_sort']=out.Priority.map(rank)
    out=out.sort_values(['_priority_sort','Score','Impressions'],ascending=[True,False,False])
    out=out.drop(columns=['_priority_sort']).reset_index(drop=True)
    opp=out[out.Priority.ne('None')].copy()
    return out, opp


def analyze_pages(p, cfg):
    if p.empty: return pd.DataFrame(),pd.DataFrame()
    p=p.copy()
    flags=[]; scores=[]; actions=[]; priorities=[]
    for _,r in p.iterrows():
        imp=int(r.Impressions); ctr=float(r['CTR (%)']); pos=float(r.Position)
        f=[]; score=0
        if imp>=max(300,cfg['MIN_IMPRESSIONS']):
            if pos<=10 and ctr<cfg['PAGE_LOW_CTR_PERCENT']:
                f.append('Page CTR review'); score+=30
            if 10<pos<=30:
                f.append('Page ranking improvement'); score+=26
            if pos>30:
                f.append('Low page visibility'); score+=18
            if f: score+=12 if imp>=1000 else 7
        if not f:
            priority='None'
            action=''
        elif score>=40:
            priority='High'
            action='Inspect relevant landing queries, snippet, on-page content and internal links before updating.'
        else:
            priority='Medium'
            action='Inspect page relevance and GSC query segmentation; prioritize by leads and business value.'
        flags.append('; '.join(f)); scores.append(score)
        actions.append(action); priorities.append(priority)
    p['Page Signal']=flags
    p['Score']=scores
    p['Priority']=priorities
    p['Next Action']=actions
    ranking={'High':0,'Medium':1,'Low':2,'None':3}
    p['_rank']=p.Priority.map(ranking)
    p=p.sort_values(['_rank','Score','Impressions'],ascending=[True,False,False]).drop(columns='_rank')
    return p.reset_index(drop=True),p[p.Priority.ne('None')].reset_index(drop=True)


def build_action_plan(opps, page_opps, has_mapping):
    groups = [
        ('Week 1', 'Relevance and SERP validation', 'Audit top-scored queries; check target service, geography, and SERP intent.', 'SEO strategist'),
        ('Week 1', 'Landing page verification',
         'Validate actual ranking URLs using query+page GSC data; do not infer matches from separate exports.'
         if not has_mapping else 'Check mapped top URLs and multi-page overlap signals for priority queries.',
         'SEO analyst'),
        ('Week 2', 'Technical and on-page review', 'Inspect existing service pages, heading alignment, internal links, indexability and metadata.', 'SEO analyst'),
        ('Week 2', 'High-impression visibility opportunities', 'Review high-impression terms ranking beyond position 20 before deciding whether content needs updating.', 'SEO strategist'),
        ('Week 3', 'Controlled optimizations', 'Implement approved on-page and internal-link improvements; record changed URLs and baseline metrics.', 'SEO content/technical'),
        ('Week 4', 'Measure and report', 'Compare stable date windows, check query/page changes and lead quality; refine priority scoring.', 'SEO strategist'),
    ]
    data=[]
    for week,task,detail,owner in groups:
        data.append({'Week':week,'Workstream':task,'What to Do':detail,'Owner':owner,
                     'Suggested KPI':'Relevant query impressions, clicks, qualified leads',
                     'Status':'Not started'})
    return pd.DataFrame(data)


def compute_summary(queries, opps, pages, page_opps, qp, chart, meta):
    return {
        'Queries analyzed':len(queries),
        'Pages analyzed':len(pages),
        'Keyword opportunities':len(opps),
        'High priority':int((opps.Priority=='High').sum()) if not opps.empty else 0,
        'Manual review flags':int((opps.Priority=='Review').sum()) if not opps.empty else 0,
        'Page opportunities':len(page_opps),
        'Verified keyword/page pairs':len(qp),
        'GSC date filter':meta['date_range'],
    }


def _safe_xlsx_value(val):
    if isinstance(val,(np.integer,)): val=int(val)
    if isinstance(val,(np.floating,)): val=float(val)
    if isinstance(val, (pd.Timestamp,datetime,date)): val=val.isoformat()
    if val is None or (not isinstance(val,(list,dict)) and pd.isna(val)):
        return None
    if isinstance(val,str):
        # Prevent spreadsheet formula injection from untrusted search queries.
        if val and val[0] in ('=','+','-','@'):
            return "'"+val
        return val
    return val


def make_excel(path, queries, opps, pages, page_opps, qp, plan, meta, cfg):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.workbook.properties import CalcProperties
    wb=Workbook()
    overview=wb.active
    overview.title='Executive Summary'
    navy='18273F'; teal='007D8A'; grey='61708A'; paleblue='EEF6F8'; imported='16805D'
    overview.sheet_view.showGridLines=False
    overview.merge_cells('A1:E2')
    overview['A1']='SEO OPPORTUNITY ANALYZER  /  V4'
    overview['A1'].font=Font(name='Aptos Display',size=19,bold=True,color='FFFFFF')
    overview['A1'].fill=PatternFill('solid',fgColor=navy)
    overview['A1'].alignment=Alignment(vertical='center')
    overview.row_dimensions[1].height=28
    overview.row_dimensions[2].height=22
    inputs=[('Client / brand',cfg['CLIENT_NAME']),('Source',meta['source']),
            ('Date range',meta['date_range']),('Search type',meta['search_type']),
            ('Query to page mapping',meta['mapping']),('Queries analyzed',len(queries)),
            ('Pages analyzed',len(pages)),('Keyword opportunities',len(opps)),
            ('High priority',int((opps.Priority=='High').sum()) if len(opps) else 0),
            ('Manual relevance review',int((opps.Priority=='Review').sum()) if len(opps) else 0),
            ('Page-level opportunities',len(page_opps)),('Verified query+page pairs',len(qp))]
    for i,(label,value) in enumerate(inputs,start=4):
        overview.cell(i,1,label); overview.cell(i,2,_safe_xlsx_value(value))
        overview.cell(i,1).font=Font(name='Aptos',bold=True,color=navy,size=11)
        overview.cell(i,2).font=Font(name='Aptos',color=imported,size=11)
        overview.cell(i,1).fill=PatternFill('solid',fgColor=paleblue)
        overview.row_dimensions[i].height=28
    overview['A18']='IMPORTANT DATA LIMITATION'
    overview['A18'].font=Font(name='Aptos',color='C46B13',bold=True,size=11)
    overview.merge_cells('A19:E20')
    overview['A19']='The standard GSC ZIP exports queries and pages separately. Without combined query+page data, exact landing-page matches cannot be calculated.'
    overview['A19'].alignment=Alignment(wrap_text=True,vertical='center')
    overview.row_dimensions[19].height=36
    overview.column_dimensions['A'].width=27
    overview.column_dimensions['B'].width=85
    for col in 'CDE': overview.column_dimensions[col].width=18
    sheets=[('Keyword Opportunities',opps),('Page Opportunities',page_opps),
            ('All Queries',queries),('All Pages',pages),('Query Page Pairs',qp),
            ('30-Day Action Plan',plan)]
    for title, df in sheets:
        ws=wb.create_sheet(title); ws.sheet_view.showGridLines=False
        if df is None or len(df.columns)==0:
            ws['A1']='No data available for this sheet.'
            if title=='Query Page Pairs':
                ws['A2']='To get verified query-page mapping, use GSC_API mode or upload a query+page CSV.'
            continue
        for j,c in enumerate(df.columns,1):
            cell=ws.cell(1,j,str(c)); cell.fill=PatternFill('solid',fgColor=navy)
            cell.font=Font(name='Aptos',size=10,bold=True,color='FFFFFF')
            cell.alignment=Alignment(wrap_text=True,vertical='center')
        ws.row_dimensions[1].height=33
        for i,values in enumerate(df.itertuples(index=False,name=None),start=2):
            for j,val in enumerate(values,start=1):
                cell=ws.cell(i,j,_safe_xlsx_value(val))
                col=str(df.columns[j-1])
                cell.font=Font(name='Aptos',size=10,color=imported if col in
                               ('Query','Page','Clicks','Impressions','CTR (%)','Position','Date') else navy)
                if i%2==0: cell.fill=PatternFill('solid',fgColor='F4F8FC')
                cell.alignment=Alignment(vertical='top',wrap_text=(j<=2 or col in
                          ('Suggested Next Action','Review Required','Next Action','What to Do')))
                if col in ('CTR (%)','Position','Top Page Share (%)'):
                    cell.number_format='0.00'
                if col in ('Impressions','Clicks','Score','Top Page Impressions'):
                    cell.number_format='#,##0'
            ws.row_dimensions[i].height=31 if i<1500 else 24
        ws.freeze_panes='B2'
        ws.auto_filter.ref=f'A1:{get_column_letter(len(df.columns))}{len(df)+1}'
        for j,col in enumerate(df.columns,1):
            ws.column_dimensions[get_column_letter(j)].width=(
                60 if col in ('Suggested Next Action','Review Required','Next Action','What to Do')
                else 54 if col in ('Query','Page','Top Ranking Page','Multi-page Signal','Page Match Source')
                else 27 if col in ('Opportunity Signals','Workstream','Page Signal')
                else 20 if col in ('Topic','Intent','Priority','Owner','Status','Suggested KPI')
                else 16)
    note=wb.create_sheet('Methodology')
    notes=[
       ('Input','One original GSC ZIP, a GSC CSV, or authenticated Search Analytics API.'),
       ('Exact query to page joins','Only available from a combined query+page data source. Never infer by joining separate aggregate files.'),
       ('Low CTR rule','Position <=10; impressions >= threshold; CTR below configured percentage. SERP features may explain low CTR.'),
       ('Striking distance','Average position 8-20 and impressions >= threshold. Position is averaged across searches.'),
       ('Visibility gap','Average position >20 and impressions >= visibility gap threshold. Not proof of missing content.'),
       ('Zero clicks','GSC zero-click query at positions <=20 and >= minimum impressions. Does not prove a technical problem.'),
       ('Review status','Ambiguous geography, off-service and very short name queries are not treated as automatically actionable.'),
       ('Content opportunities','Require page content/crawl review, SERP verification and business-intent validation; only flags are generated here.'),
       ('Query-page multi-URL signal','Two meaningful page appearances for a query prompt investigation, not a definitive cannibalization diagnosis.'),
       ('Reporting boundaries','GSC query rows can be anonymized/limited; sum of rows can differ from full-site totals.'),
       ('AI','No paid AI API is used; transparent rule-based screening only.'),
       ('Search Console API reference','https://developers.google.com/webmaster-tools/v1/searchanalytics/query'),
    ]
    for i,(k,v) in enumerate(notes,start=1):
        note.cell(i,1,k); note.cell(i,2,v)
        note.cell(i,1).font=Font(name='Aptos',bold=True,color=teal)
        note.cell(i,2).alignment=Alignment(wrap_text=True,vertical='center')
        note.row_dimensions[i].height=36
    note.column_dimensions['A'].width=31
    note.column_dimensions['B'].width=112
    note.sheet_view.showGridLines=False
    wb.calculation=CalcProperties(calcMode='auto')
    wb.save(path)
    return path


def make_plots(queries,opps,pages,chart):
    import plotly.graph_objects as go
    import plotly.express as px
    figures=[]
    if not opps.empty:
        counts=opps.Priority.value_counts().reindex(['High','Medium','Low','Review'],fill_value=0)
        f=go.Figure(go.Bar(x=counts.index.tolist(),y=counts.values.tolist(),
                           marker_color=['#13A39D','#388BC7','#96A4BB','#E2A04B']))
        f.update_layout(title='Keyword opportunity status',height=330,margin=dict(l=40,r=20,t=50,b=40),
                        plot_bgcolor='white',paper_bgcolor='white',yaxis_title='Flagged queries')
        figures.append(('Priority Distribution',f))
        dots=opps.head(250).copy()
        dots['Hover Query']=dots['Query'].astype(str).str.slice(0,100)
        dots['Impressions size']=np.log1p(dots['Impressions'])*4
        scatter=px.scatter(dots,x='Impressions',y='Position',color='Priority',
                           size='Impressions size',hover_name='Hover Query',
                           color_discrete_map={'High':'#13A39D','Medium':'#388BC7',
                                               'Low':'#96A4BB','Review':'#E2A04B'},
                           hover_data={'Clicks':True,'CTR (%)':':.2f','Impressions size':False},
                           title='Opportunity landscape - impressions vs average position')
        scatter.update_yaxes(autorange='reversed',title='Average Google position (lower is better)')
        scatter.update_layout(height=420,margin=dict(l=40,r=20,t=60,b=45),
                              paper_bgcolor='white',plot_bgcolor='white')
        figures.append(('Query Landscape',scatter))
        top=opps.groupby('Topic',as_index=False)['Impressions'].sum().sort_values('Impressions',ascending=False).head(9)
        f3=px.bar(top,x='Impressions',y='Topic',orientation='h',title='Impressions of flagged queries by topic')
        f3.update_yaxes(categoryorder='total ascending')
        f3.update_layout(height=360,margin=dict(l=35,r=20,t=65,b=30),paper_bgcolor='white',plot_bgcolor='white',
                         showlegend=False)
        figures.append(('Topic Opportunities',f3))
    if not chart.empty and 'Date' in chart.columns:
        ch=chart.copy()
        f=px.line(ch,x='Date',y='Clicks',markers=True,title='Google Search clicks by day')
        f.update_layout(height=330,margin=dict(l=35,r=20,t=55,b=35),paper_bgcolor='white',plot_bgcolor='white')
        figures.append(('Clicks Timeline',f))
    if not pages.empty:
        top=pages.sort_values('Impressions',ascending=False).head(10).copy()
        top['Short URL']=top.Page.astype(str).map(lambda x:x.replace('https://','').replace('http://','')[:65])
        f=px.bar(top.iloc[::-1],x='Impressions',y='Short URL',orientation='h',
                 title='Top landing pages by impressions')
        f.update_layout(height=390,margin=dict(l=30,r=25,t=60,b=40),
                        paper_bgcolor='white',plot_bgcolor='white',showlegend=False)
        figures.append(('Pages',f))
    return figures


def make_html_report(path, summary, queries, opps, pages, page_opps, chart, meta, cfg, figures):
    """Produce an offline, searchable dashboard with self-contained Plotly JS."""
    import plotly.io as pio
    client=html_lib.escape(str(cfg['CLIENT_NAME']))
    source=html_lib.escape(str(meta['source']))
    mapped=bool(summary['Verified keyword/page pairs'])
    cards=[('Queries analyzed',summary['Queries analyzed']),('Pages analyzed',summary['Pages analyzed']),
           ('Keyword opportunities',summary['Keyword opportunities']),
           ('High priority',summary['High priority']),('Review required',summary['Manual review flags']),
           ('Verified query/page pairs',summary['Verified keyword/page pairs'])]
    cards_html=''.join('<div class="stat"><div class="stat-name">'+html_lib.escape(label)+'</div><strong>'+f'{int(value):,}'+'</strong></div>'
                       for label,value in cards)
    chartparts=[]
    for ix,(name,fig) in enumerate(figures):
        chartparts.append('<div class="chart">'+pio.to_html(fig,include_plotlyjs=(True if ix==0 else False),
                                   full_html=False,config={'displaylogo':False,'responsive':True})+'</div>')
    if not figures:
        chartparts.append('<div class="chart">No chart data in this export.</div>')
    # data goes into JSON script payload, never directly evaluated as markup
    if not opps.empty:
        rows=opps[['Query','Clicks','Impressions','CTR (%)','Position','Priority',
                   'Topic','Top Ranking Page','Review Required','Suggested Next Action']].copy()
        rows=rows.fillna('')
        obj=rows.to_dict(orient='records')
    else: obj=[]
    raw=json.dumps(obj,ensure_ascii=True,allow_nan=False)
    raw=raw.replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    page_table=(page_opps[['Page','Clicks','Impressions','CTR (%)','Position','Priority','Next Action']]
                .head(25).to_html(index=False,escape=True,classes='page-table',border=0,float_format=lambda x:f'{x:,.2f}')
                if not page_opps.empty else '<p>No pages met the page opportunity screening rules.</p>')
    banner=('Verified mapping: query/page pairs are available.' if mapped else
          'No exact query-to-page mapping in this ZIP. Query and page exports are separate; use optional GSC API mode for verified matches.')
    template=r'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SEO Opportunity Analyzer V4 | {{CLIENT}}</title>
<style>
:root{--navy:#15283e;--ink:#183049;--muted:#63758a;--border:#dce6ef;--cyan:#04a3a6;--paper:#f5f8fc;--orange:#d4913f}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}
.shell{max-width:1280px;margin:auto;padding:34px 26px 70px}.hero{background:linear-gradient(115deg,#142c43,#19475b);border-radius:22px;padding:34px;color:white;box-shadow:0 14px 35px rgba(20,43,69,.14)}
.eyebrow{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:#83d9d4;font-weight:800}.hero h1{font-size:35px;margin:12px 0 6px;line-height:1.15}.hero p{font-size:14px;opacity:.8;margin:0}
.meta{margin-top:20px;font-size:12px;color:#c1d5e1;word-break:break-word}.stats{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin:21px 0}
.stat{background:white;border:1px solid var(--border);padding:18px;border-radius:14px}.stat-name{color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.02em}.stat strong{display:block;margin-top:11px;font-size:29px;color:var(--ink)}
.notice{border:1px solid #e6d3ad;background:#fff8e9;border-radius:13px;padding:14px 18px;color:#68532f;font-size:13px;margin:18px 0 24px}
h2{font-size:20px;margin:28px 0 12px}.sub{color:var(--muted);font-size:13px;margin-bottom:15px}.figures{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px}
.chart,.panel{background:white;border-radius:16px;border:1px solid var(--border);padding:16px;overflow:auto;box-shadow:0 4px 18px rgba(24,48,73,.03)}
.chart:nth-child(2){grid-column:span 1}.controls{display:flex;gap:12px;flex-wrap:wrap;margin:8px 0 17px}
input,select{background:#fff;border:1px solid #cedbe6;padding:12px 14px;border-radius:9px;font:inherit;font-size:13px;color:var(--ink)}input{min-width:290px;flex:2}select{flex:1;min-width:140px}
table{width:100%;border-collapse:collapse;font-size:12px}th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;background:#eef4f9;color:#4f677d;position:sticky;top:0;padding:12px;white-space:nowrap}
td{border-bottom:1px solid #e8eef4;padding:12px;vertical-align:top;max-width:290px;overflow-wrap:anywhere}tr:hover td{background:#f8fbfd}.pill{border-radius:99px;padding:4px 8px;font-weight:700;background:#e9eff6;color:#50617a;display:inline-block}
.pill.high{background:#ddf7f1;color:#127e72}.pill.medium{background:#e2f0fd;color:#24669e}.pill.review{background:#fff0da;color:#a46b21}
.small{font-size:12px;color:#687990}.count{font-size:12px;color:#63758a;margin-bottom:9px}.footer{margin:25px 0;font-size:12px;color:#7f8ea0}
@media(max-width:1100px){.stats{grid-template-columns:repeat(3,1fr)}}@media(max-width:780px){.shell{padding:12px}.stats{grid-template-columns:repeat(2,1fr)}.figures{grid-template-columns:1fr}.hero h1{font-size:26px}.hero{padding:22px}}
</style></head><body><main class="shell"><header class="hero"><div class="eyebrow">Independent SEO research / v3</div><h1>SEO Opportunity Analyzer</h1><p>{{CLIENT}} - search intelligence, opportunities and action priorities</p><div class="meta">{{DATE}} | {{SOURCE}}</div></header>
<div class="stats">{{CARDS}}</div>
<div class="notice"><strong>Data quality:</strong> {{NOTICE}} Query-level counts do not represent all site traffic. Rankings are averages; flags require human validation.</div>
<h2>Performance signals</h2><p class="sub">Explore live charts and hover for details.</p><section class="figures">{{CHARTS}}</section>
<h2>Keyword opportunities</h2><p class="sub">Filter by relevance and priority. This is a screening list, not a list of confirmed SEO problems.</p>
<div class="controls"><input id="search" placeholder="Search a keyword, page or topic..." aria-label="Search keyword"><select id="priority" aria-label="Priority"><option value="">All priorities</option><option>High</option><option>Medium</option><option>Low</option><option>Review</option></select><select id="topic" aria-label="Topic"><option value="">All topics</option></select></div>
<div class="panel"><div class="count" id="recordCount"></div><div style="max-height:570px;overflow:auto"><table id="queryTable"><thead><tr><th>Query</th><th>Impressions</th><th>Clicks</th><th>CTR</th><th>Position</th><th>Priority</th><th>Ranking page</th><th>Suggested action</th></tr></thead><tbody id="queryBody"></tbody></table></div></div>
<h2>Page-level opportunities</h2><p class="sub">These are page aggregate metrics from the GSC Pages export; they do not imply a particular query ranks for the page.</p><div class="panel">{{PAGE_TABLE}}</div>
<div class="footer">Built with Google Search Console data. No paid AI API. Verify SERP intent, on-page issues, actual landing pages and lead quality before client-facing recommendations.</div></main>
<script type="application/json" id="records">{{DATA_JSON}}</script>
<script>
(function(){const q=JSON.parse(document.getElementById('records').textContent),body=document.getElementById('queryBody'),search=document.getElementById('search'),priority=document.getElementById('priority'),topic=document.getElementById('topic'),count=document.getElementById('recordCount');
[...new Set(q.map(x=>x.Topic).filter(Boolean))].sort().forEach(t=>{let option=document.createElement('option');option.value=t;option.textContent=t;topic.appendChild(option)});
function addCell(tr,text){const t=document.createElement('td');t.textContent=text==null?'':String(text);tr.appendChild(t);return t}
function draw(){const phrase=search.value.trim().toLowerCase();let hits=q.filter(x=>(!priority.value||x.Priority===priority.value)&&(!topic.value||x.Topic===topic.value)&&(!phrase||String(x.Query+' '+x.Topic+' '+x['Top Ranking Page']).toLowerCase().includes(phrase)));body.textContent='';count.textContent=hits.length+' matching queries (showing up to 250; Excel contains all rows).';hits.slice(0,250).forEach(x=>{let tr=document.createElement('tr');addCell(tr,x.Query);addCell(tr,Number(x.Impressions).toLocaleString());addCell(tr,x.Clicks);addCell(tr,Number(x['CTR (%)']).toFixed(2)+'%');addCell(tr,Number(x.Position).toFixed(1));let cell=document.createElement('td');let tag=document.createElement('span');tag.className='pill '+String(x.Priority).toLowerCase();tag.textContent=x.Priority;cell.appendChild(tag);tr.appendChild(cell);addCell(tr,x['Top Ranking Page']);addCell(tr,x['Suggested Next Action']);body.appendChild(tr)})}
[search,priority,topic].forEach(e=>e.addEventListener('input',draw));draw();})();
</script></body></html>'''
    replacements={'{{CLIENT}}':client,'{{SOURCE}}':source,
                  '{{DATE}}':html_lib.escape(meta['date_range']),
                  '{{CARDS}}':cards_html,'{{CHARTS}}':''.join(chartparts),
                  '{{NOTICE}}':html_lib.escape(banner),
                  '{{PAGE_TABLE}}':page_table,'{{DATA_JSON}}':raw}
    for key,val in replacements.items(): template=template.replace(key,val)
    with open(path,'w',encoding='utf-8') as f: f.write(template)
    return path


def save_opportunity_csv(path,opps):
    x=opps.copy()
    for col in x.select_dtypes(include=['object','string']).columns:
        x[col] = x[col].map(lambda val: "'"+val if isinstance(val,str) and val and val[0] in '=+-@' else val)
    x.to_csv(path,index=False,encoding='utf-8-sig')
    return path

import csv, io, os, urllib.request, datetime, pathlib, subprocess
ROOT=pathlib.Path(__file__).resolve().parents[1]; DATA=ROOT/'data'; DATA.mkdir(exist_ok=True)
URL='https://raw.githubusercontent.com/rahilmittal1/gpu-price-history/main/data/rent/rent_panel_monthly.csv'
TARGET=DATA/'historical_monthly.csv'
req=urllib.request.Request(URL,headers={'User-Agent':'GPU-Tracker-Research/1.0'})
with urllib.request.urlopen(req,timeout=120) as response: raw=response.read()
rows=list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
required={'month','provider','gpu','form_factor','segment','region','usd_per_gpu_hour','source','snapshot_date','in_index'}
if not rows or not required.issubset(rows[0]): raise ValueError('Unexpected upstream schema')
providers={'aws','azure','gcp','oci'}; gpus={'H100','H200','B200','B300'}
rows=[r for r in rows if r['provider'].lower() in providers and r['gpu'].upper() in gpus and r['segment']=='hyperscaler_on_demand' and r['in_index'].lower()=='true']
if not rows: raise ValueError('No eligible rows; upstream schema may have changed')
fields=list(rows[0]); out=io.StringIO(newline=''); writer=csv.DictWriter(out,fieldnames=fields); writer.writeheader(); writer.writerows(sorted(rows,key=lambda r:(r['month'],r['gpu'],r['provider'],r['region'],r['form_factor'])))
new=out.getvalue(); old=TARGET.read_text(encoding='utf-8') if TARGET.exists() else None
if old!=new: TARGET.write_text(new,encoding='utf-8')
# A weekly run checks upstream, but does not manufacture weekly observations from monthly data.
status=DATA/'update_status.csv'; timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
with status.open('w',newline='',encoding='utf-8') as f:
 w=csv.writer(f);w.writerow(['checked_at_utc','upstream_last_month','eligible_rows','changed','frequency','source_url']);w.writerow([timestamp,max(r['month'] for r in rows),len(rows),old!=new,'monthly',URL])
print(f'Checked {timestamp}; latest month {max(r["month"] for r in rows)}; {len(rows)} rows; changed={old!=new}')

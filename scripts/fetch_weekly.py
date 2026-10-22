"""Independent US GPU on-demand price snapshots; Python standard library only.

Conservative allowlists: unknown SKUs are excluded, never guessed.
"""
import csv
import datetime as dt
import io
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
OUT = DATA / 'weekly_prices.csv'
STATUS = DATA / 'weekly_update_status.csv'
FIELDS = ['observed_at_utc','week_start','provider','gpu','region','instance_type','gpu_count','vm_usd_per_hour','usd_per_gpu_hour','pricing_type','source_url','source_sku','source_effective_date','status']
# Validate additional SKUs and GPU counts against vendor specs before extending.
AWS_TYPES = {'p5.48xlarge': ('H100',8), 'p5e.48xlarge': ('H200',8)}
AZURE_TYPES = {'Standard_ND96isr_H100_v5': ('H100',8), 'Standard_ND96isr_H200_v5': ('H200',8)}
AWS_URL = 'https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.csv'
AZURE_BASE = 'https://prices.azure.com/api/retail/prices'

def fetch(url, timeout=180):
    req=urllib.request.Request(url,headers={'User-Agent':'GPU-Price-Research/1.0','Accept':'application/json,text/csv'})
    return urllib.request.urlopen(req,timeout=timeout)

def aws_quotes():
    # AWS region-specific price list can be large. Read line-by-line; never load entire file.
    result=[]
    with fetch(AWS_URL,timeout=300) as response:
        stream=io.TextIOWrapper(response,encoding='utf-8-sig',newline='')
        # AWS bulk CSV includes metadata lines before its header.
        for line in stream:
            if line.startswith('SKU,'):
                header=next(csv.reader([line])); break
        else: raise RuntimeError('AWS CSV header not found')
        reader=csv.DictReader(stream,fieldnames=header)
        for r in reader:
            typ=r.get('Instance Type','')
            if typ not in AWS_TYPES: continue
            if r.get('TermType')!='OnDemand': continue
            if r.get('Operating System')!='Linux' or r.get('Tenancy')!='Shared': continue
            if r.get('Pre Installed S/W') not in ('NA',''): continue
            if r.get('Capacity Status') not in ('Used',''): continue
            if r.get('Unit') not in ('Hrs','Hours'): continue
            if r.get('PricePerUnit','') in ('','0.0000000000'): continue
            # Avoid additional software/fee products and non-compute usage.
            if r.get('Product Family')!='Compute Instance': continue
            if 'BoxUsage' not in r.get('usageType','') and 'BoxUsage' not in r.get('Usage Type',''): continue
            try: price=float(r['PricePerUnit'])
            except (ValueError,KeyError): continue
            if price<=0: continue
            gpu,count=AWS_TYPES[typ]
            result.append(dict(provider='aws',gpu=gpu,region='us-east-1',instance_type=typ,gpu_count=count,vm_usd_per_hour=price,usd_per_gpu_hour=price/count,pricing_type='on_demand',source_url=AWS_URL,source_sku=r.get('SKU',''),source_effective_date=r.get('EffectiveDate',''),status='ok'))
    # Fail closed if multiple inconsistent rates for the same VM.
    unique={}
    for r in result:
        key=(r['region'],r['instance_type'])
        if key in unique and unique[key]['vm_usd_per_hour']!=r['vm_usd_per_hour']:
            raise RuntimeError(f'AWS conflicting rates for {key}')
        unique[key]=r
    return list(unique.values())

def azure_quotes():
    result=[]
    for sku,(gpu,count) in AZURE_TYPES.items():
        filt="serviceName eq 'Virtual Machines' and armRegionName eq 'eastus' and armSkuName eq '%s' and priceType eq 'Consumption'" % sku
        url=AZURE_BASE+'?'+urllib.parse.urlencode({'$filter':filt,'currencyCode':'USD'})
        next_url=url
        seen=set()
        while next_url:
            if next_url in seen: raise RuntimeError('Azure pagination loop')
            seen.add(next_url)
            with fetch(next_url) as resp: payload=json.load(resp)
            for item in payload.get('Items',[]):
                if item.get('armSkuName')!=sku or item.get('armRegionName')!='eastus': continue
                if item.get('type')!='Consumption' or item.get('currencyCode')!='USD': continue
                if item.get('unitOfMeasure')!='1 Hour': continue
                if item.get('isPrimaryMeterRegion') is False: continue
                description=' '.join(str(item.get(k,'')) for k in ('productName','skuName','meterName'))
                if any(x in description.lower() for x in ('spot','low priority','windows','promo')): continue
                try: price=float(item['retailPrice'])
                except (KeyError,TypeError,ValueError): continue
                if price<=0: continue
                result.append(dict(provider='azure',gpu=gpu,region='eastus',instance_type=sku,gpu_count=count,vm_usd_per_hour=price,usd_per_gpu_hour=price/count,pricing_type='on_demand',source_url=url,source_sku=item.get('meterId',''),source_effective_date=item.get('effectiveStartDate',''),status='ok'))
            next_url=payload.get('NextPageLink')
    unique={}
    for r in result:
        key=(r['region'],r['instance_type'])
        if key in unique and unique[key]['vm_usd_per_hour']!=r['vm_usd_per_hour']:
            raise RuntimeError(f'Azure conflicting rates for {key}')
        unique[key]=r
    return list(unique.values())

def main():
    now=dt.datetime.now(dt.timezone.utc)
    stamp=now.isoformat(timespec='seconds')
    week=(now.date()-dt.timedelta(days=now.weekday())).isoformat()
    prior=[]
    if OUT.exists():
        with OUT.open(newline='',encoding='utf-8') as f: prior=list(csv.DictReader(f))
    rows=[]; statuses=[]
    for name,fn in [('aws',aws_quotes),('azure',azure_quotes)]:
        try:
            new=fn()
            statuses.append((name,'ok' if new else 'no_matching_sku',len(new),'',stamp))
            rows.extend(new)
        except Exception as exc:
            statuses.append((name,'error',0,str(exc)[:300],stamp))
    # Do not invent absent GPU prices. Persist only observed quotes.
    for r in rows:
        r.update(observed_at_utc=stamp,week_start=week)
    # Idempotent weekly snapshot: same provider/region/VM/week is replaced on rerun.
    keys={(r['week_start'],r['provider'],r['region'],r['instance_type']) for r in rows}
    prior=[r for r in prior if (r['week_start'],r['provider'],r['region'],r['instance_type']) not in keys]
    merged=prior+rows
    merged.sort(key=lambda r:(r['week_start'],r['provider'],r['gpu'],r['region'],r['instance_type']))
    tmp=OUT.with_suffix('.tmp')
    with tmp.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(merged)
    os.replace(tmp,OUT)
    with STATUS.open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f);w.writerow(['provider','status','quote_count','error','checked_at_utc']);w.writerows(statuses)
    for item in statuses: print(*item,sep=' | ')
    if any(s[1]=='error' for s in statuses): raise SystemExit('One or more API sources failed; check status CSV')
    if not rows: raise SystemExit('No verified prices found; review SKU mappings and API response')

if __name__=='__main__': main()

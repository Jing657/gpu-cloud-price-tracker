

"""Collect independent weekly US AWS/Azure GPU on-demand price observations.

Standard library only. Unknown SKUs are never inferred. Existing monthly data is untouched.
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
FIELDS = ['observed_at_utc','week_start','provider','gpu','region','instance_type',
          'gpu_count','vm_usd_per_hour','usd_per_gpu_hour','pricing_type','source_url',
          'source_sku','source_effective_date','status']
AWS_TYPES = {'p5.48xlarge': ('H100', 8), 'p5e.48xlarge': ('H200', 8)}
AZURE_TYPES = {'Standard_ND96isr_H100_v5': ('H100', 8),
               'Standard_ND96isr_H200_v5': ('H200', 8)}
AWS_URL = 'https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.csv'
AZURE_BASE = 'https://prices.azure.com/api/retail/prices'


def fetch(url, timeout=180):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'GPU-Price-Research/1.0',
        'Accept': 'application/json,text/csv'})
    return urllib.request.urlopen(request, timeout=timeout)


def field(row, *names):
    """Case-insensitive column lookup, accounting for spaces and punctuation."""
    normalized = {''.join(c for c in str(k).lower() if c.isalnum()): v
                  for k, v in row.items() if k is not None}
    for name in names:
        key = ''.join(c for c in name.lower() if c.isalnum())
        if key in normalized and normalized[key] is not None:
            return str(normalized[key]).strip()
    return ''


def aws_quotes():
    quotes = []
    counts = {'matching_instance': 0, 'on_demand': 0, 'linux_shared': 0,
              'compute_family': 0, 'box_usage': 0, 'positive_hourly': 0}
    candidates = set()
    with fetch(AWS_URL, timeout=300) as response:
        stream = io.TextIOWrapper(response, encoding='utf-8-sig', newline='')
        reader = csv.reader(stream)
        header = None
        for record in reader:
            cleaned = [x.strip().lstrip('\ufeff') for x in record]
            if 'SKU' in cleaned and ('Instance Type' in cleaned or 'instanceType' in cleaned):
                header = cleaned
                break
        if header is None:
            raise RuntimeError('AWS CSV header not found (SKU + Instance Type); check source format')
        print('AWS header columns:', header, flush=True)
        for values in reader:
            if len(values) != len(header):
                continue
            r = dict(zip(header, values))
            typ = field(r, 'Instance Type', 'instanceType')
            if typ not in AWS_TYPES:
                continue
            counts['matching_instance'] += 1
            candidates.add(typ)
            if field(r, 'TermType', 'Term Type').lower() != 'ondemand':
                continue
            counts['on_demand'] += 1
            if field(r, 'Operating System', 'operatingSystem').lower() != 'linux':
                continue
            if field(r, 'Tenancy').lower() != 'shared':
                continue
            if field(r, 'Pre Installed S/W', 'preInstalledSw').lower() not in ('na', ''):
                continue
            if field(r, 'Capacity Status', 'capacitystatus').lower() not in ('used', ''):
                continue
            counts['linux_shared'] += 1
            if field(r, 'Product Family', 'productFamily').lower() != 'compute instance':
                continue
            counts['compute_family'] += 1
            usage = field(r, 'UsageType', 'Usage Type', 'usageType')
            if 'BoxUsage' not in usage:
                continue
            counts['box_usage'] += 1
            if field(r, 'Unit').lower() not in ('hrs', 'hours'):
                continue
            try:
                price = float(field(r, 'PricePerUnit', 'Price Per Unit'))
            except ValueError:



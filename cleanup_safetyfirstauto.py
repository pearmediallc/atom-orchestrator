"""One-shot orphan-resource cleanup for safetyfirstauto.pro.

Why this exists: 2026-05-05 sandbox setup_domain attempt failed at Step 4
(S3 PutBucketWebsite blocked by Org SCP). Steps 1-3 had already succeeded
and left orphaned ACM cert, Route 53 zone, and one or more S3 buckets in
the sandbox account. The SCP was relaxed overnight 2026-05-05 → 2026-05-06,
so deletion now works. This script cleans up.

Default mode is DRY RUN — it lists what it would delete and exits.
Pass --confirm to actually delete.

Prerequisites:
    pip install boto3
    export AWS_ACCESS_KEY_ID=...
    export AWS_SECRET_ACCESS_KEY=...
    (sandbox account 901607650748)

Usage:
    python cleanup_safetyfirstauto.py            # dry run
    python cleanup_safetyfirstauto.py --confirm  # actually delete

What this script does NOT touch:
    • Namecheap validation CNAME on safetyfirstauto.pro — harmless cosmetic;
      remove via Namecheap web UI if you care.
    • Anything outside the sandbox account.

This is a single-target one-shot — delete this file once cleanup is done.
"""
import argparse
import sys

DOMAIN = 'safetyfirstauto.pro'
EXPECTED_ACCOUNT = '901607650748'  # sandbox; refuse to run against anything else
REGION = 'us-east-1'  # ATOM's default region for ACM (CloudFront requires us-east-1) + S3


def _import_boto3():
    try:
        import boto3
        from botocore.exceptions import ClientError
        return boto3, ClientError
    except ImportError:
        sys.exit('boto3 is not installed. Run: pip install boto3')


def verify_account(boto3) -> str:
    """Confirm we're hitting the sandbox account before doing anything destructive."""
    sts = boto3.client('sts')
    ident = sts.get_caller_identity()
    account = ident['Account']
    arn = ident['Arn']
    print(f'Authenticated as: {arn}')
    print(f'Account ID:       {account}')
    if account != EXPECTED_ACCOUNT:
        sys.exit(
            f'\nREFUSING TO RUN: expected account {EXPECTED_ACCOUNT} (sandbox), '
            f'got {account}. Check your AWS_* env vars.'
        )
    return account


# ─── ACM ───────────────────────────────────────────────────────────────────

def find_acm_certs(boto3, domain: str) -> list:
    acm = boto3.client('acm', region_name=REGION)
    matches = []
    paginator = acm.get_paginator('list_certificates')
    for page in paginator.paginate():
        for cert in page.get('CertificateSummaryList', []):
            if cert.get('DomainName') == domain or domain in (cert.get('SubjectAlternativeNameSummaries') or []):
                matches.append(cert)
    return matches


def delete_acm_cert(boto3, ClientError, arn: str) -> None:
    acm = boto3.client('acm', region_name=REGION)
    try:
        acm.delete_certificate(CertificateArn=arn)
        print(f'  ✓ Deleted ACM cert {arn}')
    except ClientError as e:
        print(f'  ✗ Failed to delete ACM cert {arn}: {e}')


# ─── Route 53 ──────────────────────────────────────────────────────────────

def find_route53_zones(boto3, domain: str) -> list:
    r53 = boto3.client('route53')
    target = domain.rstrip('.') + '.'
    matches = []
    paginator = r53.get_paginator('list_hosted_zones')
    for page in paginator.paginate():
        for zone in page.get('HostedZones', []):
            if zone.get('Name') == target:
                matches.append(zone)
    return matches


def delete_route53_zone(boto3, ClientError, zone_id: str, zone_name: str) -> None:
    r53 = boto3.client('route53')
    # Must remove all non-default (NS, SOA) record sets first, then delete zone.
    try:
        paginator = r53.get_paginator('list_resource_record_sets')
        to_delete = []
        for page in paginator.paginate(HostedZoneId=zone_id):
            for rrset in page.get('ResourceRecordSets', []):
                if rrset['Type'] in ('NS', 'SOA') and rrset['Name'] == zone_name:
                    continue  # leave the zone-default NS+SOA so DELETE works
                to_delete.append(rrset)

        if to_delete:
            changes = [{'Action': 'DELETE', 'ResourceRecordSet': rs} for rs in to_delete]
            r53.change_resource_record_sets(
                HostedZoneId=zone_id,
                ChangeBatch={'Changes': changes},
            )
            print(f'  ✓ Removed {len(to_delete)} record set(s) from zone {zone_id}')

        r53.delete_hosted_zone(Id=zone_id)
        print(f'  ✓ Deleted Route 53 zone {zone_id} ({zone_name})')
    except ClientError as e:
        print(f'  ✗ Failed to delete zone {zone_id}: {e}')


# ─── S3 ────────────────────────────────────────────────────────────────────

def find_s3_buckets(boto3, ClientError, domain: str) -> list:
    """ATOM names buckets exactly after the apex + www subdomain. Probe both."""
    s3 = boto3.client('s3')
    candidates = [domain, f'www.{domain}']
    matches = []
    for name in candidates:
        try:
            s3.head_bucket(Bucket=name)
            matches.append(name)
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code', '')
            if code in ('404', 'NoSuchBucket', 'NotFound'):
                continue
            # Any other error (403, etc.) — surface but don't add to delete list
            print(f'  ! Could not check bucket {name}: {e}')
    return matches


def delete_s3_bucket(boto3, ClientError, name: str) -> None:
    s3_resource = boto3.resource('s3')
    bucket = s3_resource.Bucket(name)
    try:
        # Empty bucket first (versioned + unversioned objects).
        bucket.object_versions.delete()
        bucket.objects.all().delete()
        bucket.delete()
        print(f'  ✓ Deleted S3 bucket {name}')
    except ClientError as e:
        print(f'  ✗ Failed to delete bucket {name}: {e}')


# ─── main ──────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--confirm', action='store_true',
                        help='Actually delete (default is dry-run)')
    args = parser.parse_args()

    boto3, ClientError = _import_boto3()

    print(f'Target domain: {DOMAIN}')
    print(f'Mode: {"DELETE" if args.confirm else "DRY RUN — nothing will be deleted"}')
    print('-' * 64)

    verify_account(boto3)
    print('-' * 64)

    # 1. ACM
    print('ACM certificates:')
    certs = find_acm_certs(boto3, DOMAIN)
    if not certs:
        print('  (none found)')
    for c in certs:
        print(f'  • {c["CertificateArn"]}  status={c.get("Status", "?")}')

    # 2. Route 53
    print('\nRoute 53 hosted zones:')
    zones = find_route53_zones(boto3, DOMAIN)
    if not zones:
        print('  (none found)')
    for z in zones:
        print(f'  • {z["Id"]}  name={z["Name"]}  records={z.get("ResourceRecordSetCount", "?")}')

    # 3. S3
    print('\nS3 buckets:')
    buckets = find_s3_buckets(boto3, ClientError, DOMAIN)
    if not buckets:
        print('  (none found)')
    for b in buckets:
        print(f'  • {b}')

    # 4. Apply (or skip)
    if not (certs or zones or buckets):
        print('\nNothing to clean up. ✅')
        return 0

    if not args.confirm:
        print('\nDRY RUN complete. Re-run with --confirm to actually delete.')
        print('Reminder: also remove the Namecheap _-prefix validation CNAME on '
              f'{DOMAIN} via the Namecheap web UI if you want a fully clean slate.')
        return 0

    print('\nDeleting...')
    for c in certs:
        delete_acm_cert(boto3, ClientError, c['CertificateArn'])
    for z in zones:
        delete_route53_zone(boto3, ClientError, z['Id'], z['Name'])
    for b in buckets:
        delete_s3_bucket(boto3, ClientError, b)

    print('\nCleanup complete.')
    print('Reminder: the Namecheap validation CNAME on '
          f'{DOMAIN} is NOT removed by this script — '
          'remove it manually in the Namecheap web UI if you want.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

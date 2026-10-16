import { describe, expect, it } from 'vitest'
import { presign } from './presign'

// AWS's published example (S3 "Authenticating Requests: Using Query Parameters (AWS Signature Version 4)").
describe('presign', () => {
  it('matches AWS’s presigned GET example byte for byte', async () => {
    expect(await presign({
      method: 'GET',
      url: 'https://examplebucket.s3.amazonaws.com/test.txt',
      accessKeyId: 'AKIAIOSFODNN7EXAMPLE',
      secretAccessKey: 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
      region: 'us-east-1',
      expires: 86400,
      now: new Date('2013-05-24T00:00:00Z'),
    })).toBe(
      'https://examplebucket.s3.amazonaws.com/test.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256'
      + '&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request'
      + '&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host'
      + '&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404',
    )
  })
  it('refuses an out-of-range lifetime', async () => {
    const o = { method: 'PUT' as const, url: 'https://x.test/b/k', accessKeyId: 'a', secretAccessKey: 's', region: 'auto', now: new Date(0) }
    await expect(presign({ ...o, expires: 0 })).rejects.toThrow('presign: expires 0 out of range')
    await expect(presign({ ...o, expires: 604801 })).rejects.toThrow('presign: expires 604801 out of range')
  })
})

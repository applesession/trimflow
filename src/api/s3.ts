import { S3Client, PutObjectCommand } from "@aws-sdk/client-s3";

let client: S3Client | null = null;

function getClient(): S3Client {
  if (client) return client;
  client = new S3Client({
    endpoint: Bun.env.S3_ENDPOINT,
    region: Bun.env.S3_REGION ?? "us-east-1",
    credentials: {
      accessKeyId: Bun.env.S3_ACCESS_KEY_ID ?? "",
      secretAccessKey: Bun.env.S3_SECRET_ACCESS_KEY ?? "",
    },
    requestHandler: {
      requestTimeout: 3600_000,
    },
    maxAttempts: 5,
  } as never);
  return client;
}

export async function uploadFileToS3(localPath: string, s3Key: string): Promise<void> {
  const c = getClient();
  const bucket = Bun.env.S3_BUCKET_NAME ?? "";

  const file = Bun.file(localPath);
  const body = await file.arrayBuffer();

  console.log(`[S3 UPLOAD] ${localPath} -> s3://${bucket}/${s3Key}`);

  await c.send(
    new PutObjectCommand({
      Bucket: bucket,
      Key: s3Key,
      Body: new Uint8Array(body),
    }),
  );
}

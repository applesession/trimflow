import { loadConfig } from "./shared/config";
import { initDb } from "./shared/db";
import { discoverJobs } from "./modules/autojobs";

initDb();
const config = loadConfig();

console.log("[DISCOVERY] Starting...");
const result = await discoverJobs();

console.log("\n[DISCOVERY SUMMARY]");
console.log(JSON.stringify(result.summary, null, 2));

if (result.summary.created_jobs > 0) {
  console.log("\n[CREATED JOBS]");
  for (const job of result.jobs.slice(-result.summary.created_jobs)) {
    console.log(`  - ${job.title} [S${job.season}] ${job.episodes_range}`);
  }
}

console.log(`\n[DISCOVERY] Done. Total jobs in queue: ${result.jobs.length}`);

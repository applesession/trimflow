import { loadConfig } from "./shared/config";
import { initDb, getJobs } from "./shared/db";
import { validateRequiredEnv, validateRequiredTools, validateRequiredFiles } from "./shared/validation";
import { runJobs } from "./core/runner";

const db = initDb();
const config = loadConfig();
const jobs = getJobs();

if (jobs.length === 0) {
  console.log("No jobs found. Queue is empty. Use /add via Telegram bot or run discovery.");
  process.exit(0);
}

console.log(`Found ${jobs.length} job(s) in queue`);

validateRequiredEnv(config, jobs);
validateRequiredTools(config, jobs);
validateRequiredFiles(config);

const summary = await runJobs(config, jobs);
console.log("\nSummary:", JSON.stringify(summary, null, 2));

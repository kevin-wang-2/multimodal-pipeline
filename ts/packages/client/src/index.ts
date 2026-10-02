export { MmpClient } from "./client.js";
export type { ClientOptions, WaitOptions, JobStatusResponse } from "./client.js";
export { MmpError } from "./errors.js";
export type { ClientErrorCode } from "./errors.js";
export { media } from "./media.js";
export { describeCapability, matchesMediaType, resolveCapability } from "./capabilities.js";
export type { CapabilityQuery } from "./capabilities.js";
export type { JobRequest, JobDone, Digest, MediaHandle, Capability, CapabilitiesResponse, HealthResponse } from "@mmp/protocol";

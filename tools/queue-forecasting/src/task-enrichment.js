// Task definition -> queue_forecast_tasks enrichment fields. Shared by the
// collector (backgroundApiFetch) and the playground API, so an on-demand
// prediction sees the same metadata_name / normalized_name / repo_family the
// collector would have stored.
import { deriveRepoFamily } from './repo-family.js';
import { normalizeMetadataName } from './utils.js';

/**
 * @param {object} taskDef  Queue.task() response
 * @param {object} [status] Queue status (fallback for ids missing from taskDef)
 */
export function taskDefToEnrichment(taskDef, status = {}) {
  const rf = deriveRepoFamily({
    routes: taskDef.routes || [],
    metadataSource: taskDef.metadata?.source ?? null,
    schedulerId: taskDef.schedulerId || status.schedulerId || null,
  });
  const metadataName = taskDef.metadata?.name || null;
  return {
    metadata_name: metadataName,
    normalized_name: normalizeMetadataName(metadataName),
    tags: taskDef.tags && Object.keys(taskDef.tags).length > 0 ? taskDef.tags : null,
    task_created: taskDef.created || null,
    original_priority: taskDef.priority || null,
    task_queue_id: taskDef.taskQueueId || status.taskQueueId,
    task_group_id: taskDef.taskGroupId || status.taskGroupId,
    scheduler_id: taskDef.schedulerId || status.schedulerId,
    project_id: taskDef.projectId || status.projectId,
    max_run_time_s: taskDef.payload?.maxRunTime ?? null,
    repo_family: rf.family,
    repo_family_source: rf.source,
    repo_family_evidence: rf.evidence,
    repo_family_derivation_version: rf.version,
    requires: taskDef.requires || null,
    deadline: taskDef.deadline || null,
    dependencies: Array.isArray(taskDef.dependencies) ? taskDef.dependencies : [],
  };
}

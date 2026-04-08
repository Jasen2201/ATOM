//! Model discovery step for external API endpoints.

use std::{collections::HashMap, time::Duration};

use async_trait::async_trait;
use once_cell::sync::Lazy;
use regex::Regex;
use reqwest::Client;
use serde::Deserialize;
use tracing::{debug, info};
use wfaas::{StepExecutor, StepId, StepResult, WorkflowContext, WorkflowError, WorkflowResult};

use crate::core::steps::workflow_data::ExternalWorkerWorkflowData;

// HTTP client for API calls
static HTTP_CLIENT: Lazy<Client> = Lazy::new(|| {
    Client::builder()
        .timeout(Duration::from_secs(30))
        .build()
        .expect("Failed to create HTTP client")
});

// Regex to strip date suffix: -YYYY-MM-DD or -YYYY-MM
static DATE_SUFFIX_PATTERN: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"-\d{4}-\d{2}(-\d{2})?$").expect("Invalid date regex"));

/// OpenAI /v1/models response format.
#[derive(Debug, Clone, Deserialize)]
pub struct ModelsResponse {
    pub data: Vec<ModelInfo>,
    #[serde(default)]
    pub object: String,
}

/// Individual model information from /v1/models.
#[derive(Debug, Clone, Deserialize)]
pub struct ModelInfo {
    pub id: String,
    #[serde(default)]
    pub object: String,
    #[serde(default)]
    pub created: Option<u64>,
    #[serde(default)]
    pub owned_by: Option<String>,
}

/// Group models by base name (stripping date suffixes), returning deduplicated primary model IDs.
///
/// # Example
/// Input:  `["gpt-4o", "gpt-4o-2024-05-13", "gpt-4o-2024-08-06", "gpt-4o-2024-11-20"]`
/// Output: `["gpt-4o"]`
pub fn group_model_ids(models: Vec<ModelInfo>) -> Vec<String> {
    // Group model IDs by base name (with date stripped)
    let mut groups: HashMap<String, Vec<String>> = HashMap::new();
    for model in &models {
        let base = DATE_SUFFIX_PATTERN.replace(&model.id, "").to_string();
        groups.entry(base).or_default().push(model.id.clone());
    }

    // Return the shortest (base) name from each group
    groups
        .into_values()
        .map(|mut variants| {
            // Sort: shortest first (base name), then alphabetically
            variants.sort_by(|a, b| a.len().cmp(&b.len()).then_with(|| a.cmp(b)));
            variants.remove(0) // shortest = primary ID
        })
        .collect()
}

/// Fetch models from /v1/models endpoint.
async fn fetch_models(url: &str, api_key: Option<&str>) -> Result<Vec<String>, String> {
    let base_url = url.trim_end_matches('/');
    let models_url = format!("{}/v1/models", base_url);

    let mut req = HTTP_CLIENT.get(&models_url);
    if let Some(key) = api_key {
        req = req.bearer_auth(key);
    }

    let response = req
        .send()
        .await
        .map_err(|e| format!("Failed to connect to {}: {}", models_url, e))?;

    if !response.status().is_success() {
        return Err(format!(
            "Server returned status {} from {}",
            response.status(),
            models_url
        ));
    }

    let models_response: ModelsResponse = response
        .json()
        .await
        .map_err(|e| format!("Failed to parse models response: {}", e))?;

    debug!(
        "Fetched {} raw models from {}",
        models_response.data.len(),
        url
    );

    let model_ids = group_model_ids(models_response.data);

    debug!(
        "Grouped into {} unique model IDs",
        model_ids.len()
    );

    Ok(model_ids)
}

/// Step 1: Discover models from external /v1/models endpoint.
pub struct DiscoverModelsStep;

#[async_trait]
impl StepExecutor<ExternalWorkerWorkflowData> for DiscoverModelsStep {
    async fn execute(
        &self,
        context: &mut WorkflowContext<ExternalWorkerWorkflowData>,
    ) -> WorkflowResult<StepResult> {
        let config = &context.data.config;

        // If no API key is provided, skip model discovery and use wildcard mode.
        if config.api_key.as_ref().is_none_or(|k| k.is_empty()) {
            info!(
                "No API key provided for {} - using wildcard mode (accepts any model). \
                 User's Authorization header will be forwarded to backend.",
                config.url
            );
            // Leave discovered_model_ids empty for wildcard mode
            return Ok(StepResult::Success);
        }

        debug!("Discovering models from external endpoint {}", config.url);

        let model_ids = fetch_models(&config.url, config.api_key.as_deref())
            .await
            .map_err(|e| WorkflowError::StepFailed {
                step_id: StepId::new("discover_models"),
                message: format!("Failed to discover models from {}: {}", config.url, e),
            })?;

        if model_ids.is_empty() {
            return Err(WorkflowError::StepFailed {
                step_id: StepId::new("discover_models"),
                message: format!("No models discovered from {}", config.url),
            });
        }

        info!(
            "Discovered {} models from {}: {:?}",
            model_ids.len(),
            config.url,
            model_ids
        );

        context.data.discovered_model_ids = model_ids;
        Ok(StepResult::Success)
    }

    fn is_retryable(&self, _error: &WorkflowError) -> bool {
        true
    }
}

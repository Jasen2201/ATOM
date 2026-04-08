use axum::{
    body::Body,
    extract::Request,
    http::{HeaderMap, HeaderValue},
};
/// Copy request headers to a Vec of name-value string pairs
/// Used for forwarding headers to backend workers
pub fn copy_request_headers(req: &Request<Body>) -> Vec<(String, String)> {
    req.headers()
        .iter()
        .filter_map(|(name, value)| {
            // Convert header value to string, skipping non-UTF8 headers
            value
                .to_str()
                .ok()
                .map(|v| (name.to_string(), v.to_string()))
        })
        .collect()
}

/// Convert headers from reqwest Response to axum HeaderMap
/// Filters out hop-by-hop headers that shouldn't be forwarded
pub fn preserve_response_headers(reqwest_headers: &HeaderMap) -> HeaderMap {
    let mut headers = HeaderMap::new();

    for (name, value) in reqwest_headers.iter() {
        // Skip hop-by-hop headers that shouldn't be forwarded
        // Use eq_ignore_ascii_case to avoid string allocation
        if should_forward_header_no_alloc(name.as_str()) {
            // The original name and value are already valid, so we can just clone them
            headers.insert(name.clone(), value.clone());
        }
    }

    headers
}

/// Determine if a header should be forwarded without allocating (case-insensitive)
fn should_forward_header_no_alloc(name: &str) -> bool {
    // List of headers that should NOT be forwarded (hop-by-hop headers)
    // Use eq_ignore_ascii_case to avoid to_lowercase() allocation
    !(name.eq_ignore_ascii_case("connection")
        || name.eq_ignore_ascii_case("keep-alive")
        || name.eq_ignore_ascii_case("proxy-authenticate")
        || name.eq_ignore_ascii_case("proxy-authorization")
        || name.eq_ignore_ascii_case("te")
        || name.eq_ignore_ascii_case("trailers")
        || name.eq_ignore_ascii_case("transfer-encoding")
        || name.eq_ignore_ascii_case("upgrade")
        || name.eq_ignore_ascii_case("content-encoding")
        || name.eq_ignore_ascii_case("host"))
}

/// Apply headers to a reqwest request builder, filtering out headers that shouldn't be forwarded
/// or that will be set automatically by reqwest
pub fn apply_request_headers(
    headers: &HeaderMap,
    mut request_builder: reqwest::RequestBuilder,
    skip_content_headers: bool,
) -> reqwest::RequestBuilder {
    // Always forward Authorization header first if present
    if let Some(auth) = headers
        .get("authorization")
        .or_else(|| headers.get("Authorization"))
    {
        request_builder = request_builder.header("Authorization", auth.clone());
    }

    // Forward other headers, filtering out problematic ones
    // Use eq_ignore_ascii_case to avoid to_lowercase() allocation per header
    for (key, value) in headers.iter() {
        let key_str = key.as_str();

        // Skip headers that:
        // - Are set automatically by reqwest (content-type, content-length for POST/PUT)
        // - We already handled (authorization)
        // - Are hop-by-hop headers (connection, transfer-encoding)
        // - Should not be forwarded (host)
        let should_skip = key_str.eq_ignore_ascii_case("authorization") // Already handled above
            || key_str.eq_ignore_ascii_case("host")
            || key_str.eq_ignore_ascii_case("connection")
            || key_str.eq_ignore_ascii_case("transfer-encoding")
            || key_str.eq_ignore_ascii_case("keep-alive")
            || key_str.eq_ignore_ascii_case("te")
            || key_str.eq_ignore_ascii_case("trailers")
            || key_str.eq_ignore_ascii_case("accept-encoding")
            || key_str.eq_ignore_ascii_case("upgrade")
            || (skip_content_headers
                && (key_str.eq_ignore_ascii_case("content-type")
                    || key_str.eq_ignore_ascii_case("content-length")));

        if !should_skip {
            request_builder = request_builder.header(key.clone(), value.clone());
        }
    }

    request_builder
}

/// API provider types for provider-specific header handling
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ApiProvider {
    Anthropic,
    Xai,
    OpenAi,
    Gemini,
    Generic,
}

impl ApiProvider {
    /// Detect provider type from URL
    pub fn from_url(url: &str) -> Self {
        if url.contains("anthropic") {
            ApiProvider::Anthropic
        } else if url.contains("x.ai") {
            ApiProvider::Xai
        } else if url.contains("openai.com") {
            ApiProvider::OpenAi
        } else if url.contains("googleapis.com") {
            ApiProvider::Gemini
        } else {
            ApiProvider::Generic
        }
    }
}

/// Apply provider-specific headers to request
pub fn apply_provider_headers(
    mut req: reqwest::RequestBuilder,
    url: &str,
    auth_header: Option<&HeaderValue>,
) -> reqwest::RequestBuilder {
    let provider = ApiProvider::from_url(url);

    match provider {
        ApiProvider::Anthropic => {
            // Anthropic requires x-api-key instead of Authorization
            // Extract Bearer token and use as x-api-key
            if let Some(auth) = auth_header {
                if let Ok(auth_str) = auth.to_str() {
                    let api_key = auth_str.strip_prefix("Bearer ").unwrap_or(auth_str);
                    req = req
                        .header("x-api-key", api_key)
                        .header("anthropic-version", "2023-06-01");
                }
            }
        }
        ApiProvider::Gemini | ApiProvider::Xai | ApiProvider::OpenAi | ApiProvider::Generic => {
            // Standard OpenAI-compatible: use Authorization header as-is
            if let Some(auth) = auth_header {
                req = req.header("Authorization", auth);
            }
        }
    }

    req
}

/// Extract auth header with passthrough semantics.
///
/// Passthrough mode: User's Authorization header takes priority.
/// Fallback: Worker's API key is used only if user didn't provide auth.
///
/// This enables use cases where:
/// 1. Users send their own API keys (multi-tenant, BYOK)
/// 2. Router has a default key for users who don't provide one
pub fn extract_auth_header(
    headers: Option<&HeaderMap>,
    worker_api_key: &Option<String>,
) -> Option<HeaderValue> {
    // Passthrough: Try user's auth header first
    let user_auth = headers.and_then(|h| {
        h.get("authorization")
            .or_else(|| h.get("Authorization"))
            .cloned()
    });

    // Return user's auth if provided, otherwise use worker's API key
    user_auth.or_else(|| {
        worker_api_key
            .as_ref()
            .and_then(|k| HeaderValue::from_str(&format!("Bearer {}", k)).ok())
    })
}

#[inline]
pub fn should_forward_request_header(name: &str) -> bool {
    const REQUEST_ID_PREFIX: &str = "x-request-id-";

    name.eq_ignore_ascii_case("authorization")
        || name.eq_ignore_ascii_case("x-request-id")
        || name.eq_ignore_ascii_case("x-correlation-id")
        || name.eq_ignore_ascii_case("traceparent")
        || name.eq_ignore_ascii_case("tracestate")
        || name
            .get(..REQUEST_ID_PREFIX.len())
            .is_some_and(|prefix| prefix.eq_ignore_ascii_case(REQUEST_ID_PREFIX))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_should_forward_request_header_whitelist() {
        assert!(should_forward_request_header("authorization"));
        assert!(should_forward_request_header("Authorization"));
        assert!(should_forward_request_header("AUTHORIZATION"));
        assert!(should_forward_request_header("x-request-id"));
        assert!(should_forward_request_header("X-Request-Id"));
        assert!(should_forward_request_header("x-correlation-id"));
        assert!(should_forward_request_header("X-Correlation-ID"));
        assert!(should_forward_request_header("traceparent"));
        assert!(should_forward_request_header("Traceparent"));
        assert!(should_forward_request_header("tracestate"));
        assert!(should_forward_request_header("Tracestate"));
        assert!(should_forward_request_header("x-request-id-user"));
        assert!(should_forward_request_header("X-Request-ID-Span"));
        assert!(should_forward_request_header("x-request-id-123"));
    }

    #[test]
    fn test_should_forward_request_header_blocked() {
        assert!(!should_forward_request_header("content-type"));
        assert!(!should_forward_request_header("Content-Type"));
        assert!(!should_forward_request_header("content-length"));
        assert!(!should_forward_request_header("host"));
        assert!(!should_forward_request_header("Host"));
        assert!(!should_forward_request_header("connection"));
        assert!(!should_forward_request_header("transfer-encoding"));
        assert!(!should_forward_request_header("accept"));
        assert!(!should_forward_request_header("accept-encoding"));
        assert!(!should_forward_request_header("user-agent"));
        assert!(!should_forward_request_header("cookie"));
        assert!(!should_forward_request_header("x-custom-header"));
        assert!(!should_forward_request_header("x-api-key"));
    }

    // ===================== should_forward_header_no_alloc tests =====================

    #[test]
    fn test_hop_by_hop_headers_filtered() {
        let hop_by_hop = [
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "upgrade",
            "content-encoding",
            "host",
        ];
        for h in hop_by_hop {
            assert!(
                !should_forward_header_no_alloc(h),
                "{h} should be filtered"
            );
        }
    }

    #[test]
    fn test_hop_by_hop_case_insensitive() {
        assert!(!should_forward_header_no_alloc("Connection"));
        assert!(!should_forward_header_no_alloc("CONNECTION"));
        assert!(!should_forward_header_no_alloc("Keep-Alive"));
        assert!(!should_forward_header_no_alloc("Transfer-Encoding"));
        assert!(!should_forward_header_no_alloc("Host"));
        assert!(!should_forward_header_no_alloc("HOST"));
    }

    #[test]
    fn test_regular_headers_forwarded() {
        let forward = [
            "content-type",
            "content-length",
            "authorization",
            "x-request-id",
            "accept",
            "user-agent",
            "x-custom-header",
        ];
        for h in forward {
            assert!(
                should_forward_header_no_alloc(h),
                "{h} should be forwarded"
            );
        }
    }

    // ===================== preserve_response_headers tests =====================

    #[test]
    fn test_preserve_response_headers_filters_hop_by_hop() {
        let mut input = HeaderMap::new();
        input.insert("content-type", HeaderValue::from_static("application/json"));
        input.insert("connection", HeaderValue::from_static("keep-alive"));
        input.insert("x-request-id", HeaderValue::from_static("abc123"));
        input.insert(
            "transfer-encoding",
            HeaderValue::from_static("chunked"),
        );

        let result = preserve_response_headers(&input);
        assert!(result.contains_key("content-type"));
        assert!(result.contains_key("x-request-id"));
        assert!(!result.contains_key("connection"));
        assert!(!result.contains_key("transfer-encoding"));
    }

    #[test]
    fn test_preserve_response_headers_empty() {
        let input = HeaderMap::new();
        let result = preserve_response_headers(&input);
        assert!(result.is_empty());
    }

    #[test]
    fn test_preserve_response_headers_all_forwardable() {
        let mut input = HeaderMap::new();
        input.insert("content-type", HeaderValue::from_static("text/plain"));
        input.insert("x-custom", HeaderValue::from_static("value"));

        let result = preserve_response_headers(&input);
        assert_eq!(result.len(), 2);
    }

    // ===================== ApiProvider tests =====================

    #[test]
    fn test_api_provider_from_url() {
        assert_eq!(
            ApiProvider::from_url("https://api.anthropic.com/v1/messages"),
            ApiProvider::Anthropic
        );
        assert_eq!(
            ApiProvider::from_url("https://api.x.ai/v1/chat/completions"),
            ApiProvider::Xai
        );
        assert_eq!(
            ApiProvider::from_url("https://api.openai.com/v1/chat/completions"),
            ApiProvider::OpenAi
        );
        assert_eq!(
            ApiProvider::from_url("https://generativelanguage.googleapis.com/v1beta"),
            ApiProvider::Gemini
        );
        assert_eq!(
            ApiProvider::from_url("http://localhost:8000/v1/chat"),
            ApiProvider::Generic
        );
    }

    #[test]
    fn test_api_provider_debug_and_traits() {
        let p = ApiProvider::Anthropic;
        assert_eq!(p, p.clone());
        let _ = format!("{:?}", p);
    }

    // ===================== extract_auth_header tests =====================

    #[test]
    fn test_extract_auth_user_header_takes_priority() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "authorization",
            HeaderValue::from_static("Bearer user-key"),
        );
        let worker_key = Some("worker-key".to_string());

        let result = extract_auth_header(Some(&headers), &worker_key);
        assert_eq!(result.unwrap().to_str().unwrap(), "Bearer user-key");
    }

    #[test]
    fn test_extract_auth_fallback_to_worker_key() {
        let headers = HeaderMap::new(); // no auth header
        let worker_key = Some("worker-key".to_string());

        let result = extract_auth_header(Some(&headers), &worker_key);
        assert_eq!(result.unwrap().to_str().unwrap(), "Bearer worker-key");
    }

    #[test]
    fn test_extract_auth_no_headers_no_key() {
        let result = extract_auth_header(None, &None);
        assert!(result.is_none());
    }

    #[test]
    fn test_extract_auth_none_headers_with_worker_key() {
        let worker_key = Some("fallback".to_string());
        let result = extract_auth_header(None, &worker_key);
        assert_eq!(result.unwrap().to_str().unwrap(), "Bearer fallback");
    }

    #[test]
    fn test_extract_auth_capital_authorization() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "Authorization",
            HeaderValue::from_static("Bearer cap-key"),
        );

        let result = extract_auth_header(Some(&headers), &None);
        assert_eq!(result.unwrap().to_str().unwrap(), "Bearer cap-key");
    }

    // ===================== copy_request_headers tests =====================

    #[test]
    fn test_copy_request_headers_basic() {
        let mut req = Request::builder();
        req = req.header("content-type", "application/json");
        req = req.header("x-custom", "value");
        let request = req.body(Body::empty()).unwrap();

        let copied = copy_request_headers(&request);
        assert!(copied.iter().any(|(k, v)| k == "content-type" && v == "application/json"));
        assert!(copied.iter().any(|(k, v)| k == "x-custom" && v == "value"));
    }

    #[test]
    fn test_copy_request_headers_empty() {
        let request = Request::builder().body(Body::empty()).unwrap();
        let copied = copy_request_headers(&request);
        assert!(copied.is_empty());
    }
}

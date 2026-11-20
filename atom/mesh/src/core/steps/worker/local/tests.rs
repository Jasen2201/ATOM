//! gRPC workers through the local worker registration workflow and its tokenizer job:
//! an ATOM worker's detection, labels, version refusal and chat template; vLLM's.

use std::{sync::Arc, time::Duration};

use mesh_grpc::vllm_proto as vllm;
use serde_json::{json, Value};
use tonic::{
    transport::{server::Router, server::TcpIncoming, Server},
    Request, Response, Status, Streaming,
};
use wfaas::WorkflowId;

use super::create_local_worker_workflow_data;
use crate::{
    app_context::AppContext,
    atom_engine::grpc::{
        atom_engine_server::{AtomEngine, AtomEngineServer},
        AbortRequest, AbortResponse, Frames, GenerateRequest, GenerateResponse,
        GetModelInfoRequest, GetModelInfoResponse, GetServerInfoRequest, GetServerInfoResponse,
        HealthCheckRequest, HealthCheckResponse, PrepareStructuredOutputRequest,
        PrepareStructuredOutputResponse,
    },
    core::{steps::WorkflowEngines, worker::RuntimeType, JobQueue, JobQueueConfig},
    protocols::{chat::ChatCompletionRequest, worker_spec::WorkerConfigRequest},
    routers::{
        prepare::prepare_chat,
        test_mocks::{app_context, tokenizer::hf_tokenizer_path},
    },
};

type Empty<T> = tokio_stream::Empty<Result<T, Status>>;

/// An ATOM instance as registration sees it: its model info and protocol version.
struct FakeAtom {
    info: GetModelInfoResponse,
    protocol_version: u32,
}

#[tonic::async_trait]
impl AtomEngine for FakeAtom {
    type GenerateStream = Empty<GenerateResponse>;
    type SessionStream = Empty<Frames>;

    async fn generate(
        &self,
        _: Request<GenerateRequest>,
    ) -> Result<Response<Self::GenerateStream>, Status> {
        Err(Status::unimplemented("generate"))
    }

    async fn abort(&self, _: Request<AbortRequest>) -> Result<Response<AbortResponse>, Status> {
        Err(Status::unimplemented("abort"))
    }

    async fn get_model_info(
        &self,
        _: Request<GetModelInfoRequest>,
    ) -> Result<Response<GetModelInfoResponse>, Status> {
        Ok(Response::new(self.info.clone()))
    }

    async fn get_server_info(
        &self,
        _: Request<GetServerInfoRequest>,
    ) -> Result<Response<GetServerInfoResponse>, Status> {
        Ok(Response::new(GetServerInfoResponse {
            protocol_version: self.protocol_version,
            ..Default::default()
        }))
    }

    async fn health_check(
        &self,
        _: Request<HealthCheckRequest>,
    ) -> Result<Response<HealthCheckResponse>, Status> {
        Ok(Response::new(HealthCheckResponse {
            healthy: true,
            message: String::new(),
        }))
    }

    async fn session(
        &self,
        _: Request<Streaming<Frames>>,
    ) -> Result<Response<Self::SessionStream>, Status> {
        Err(Status::unimplemented("session"))
    }

    async fn prepare_structured_output(
        &self,
        _: Request<PrepareStructuredOutputRequest>,
    ) -> Result<Response<PrepareStructuredOutputResponse>, Status> {
        Err(Status::unimplemented("prepare_structured_output"))
    }
}

/// A vLLM instance that answers HealthCheck and GetModelInfo.
struct FakeVllm;

#[tonic::async_trait]
impl vllm::vllm_engine_server::VllmEngine for FakeVllm {
    type GenerateStream = Empty<vllm::GenerateResponse>;

    async fn generate(
        &self,
        _: Request<vllm::GenerateRequest>,
    ) -> Result<Response<Self::GenerateStream>, Status> {
        Err(Status::unimplemented("generate"))
    }

    async fn embed(
        &self,
        _: Request<vllm::EmbedRequest>,
    ) -> Result<Response<vllm::EmbedResponse>, Status> {
        Err(Status::unimplemented("embed"))
    }

    async fn health_check(
        &self,
        _: Request<vllm::HealthCheckRequest>,
    ) -> Result<Response<vllm::HealthCheckResponse>, Status> {
        Ok(Response::new(vllm::HealthCheckResponse {
            healthy: true,
            message: String::new(),
        }))
    }

    async fn abort(
        &self,
        _: Request<vllm::AbortRequest>,
    ) -> Result<Response<vllm::AbortResponse>, Status> {
        Err(Status::unimplemented("abort"))
    }

    async fn get_model_info(
        &self,
        _: Request<vllm::GetModelInfoRequest>,
    ) -> Result<Response<vllm::GetModelInfoResponse>, Status> {
        Ok(Response::new(vllm::GetModelInfoResponse {
            model_path: hf_tokenizer_path().into(),
            max_context_length: 2048,
            ..Default::default()
        }))
    }

    async fn get_server_info(
        &self,
        _: Request<vllm::GetServerInfoRequest>,
    ) -> Result<Response<vllm::GetServerInfoResponse>, Status> {
        Err(Status::unimplemented("get_server_info"))
    }
}

/// Serves `router` on a free local port and returns its `grpc://` URL.
async fn serve(router: Router) -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("grpc://{}", listener.local_addr().unwrap());
    tokio::spawn(router.serve_with_incoming(TcpIncoming::from(listener)));
    url
}

async fn serve_atom(info: GetModelInfoResponse, protocol_version: u32) -> String {
    let fake = FakeAtom {
        info,
        protocol_version,
    };
    serve(Server::builder().add_service(AtomEngineServer::new(fake))).await
}

/// GetModelInfo of model `name`, whose tokenizer is the HF test fixture.
fn model_info(name: &str, chat_template: &str, default_kwargs: &str) -> GetModelInfoResponse {
    GetModelInfoResponse {
        model_path: format!("/models/{name}"),
        tokenizer_path: hf_tokenizer_path().into(),
        served_model_name: name.into(),
        max_context_length: 4096,
        eos_token_ids: vec![2, 7],
        chat_template: Some(chat_template.into()),
        default_chat_template_kwargs_json: default_kwargs.into(),
        reasoning_open_markers: Vec::new(),
    }
}

/// A router context with the job queue and workflow engines that registration uses.
fn context() -> Arc<AppContext> {
    let ctx = app_context();
    let queue = JobQueue::new(JobQueueConfig::default(), Arc::downgrade(&ctx));
    ctx.worker_job_queue.set(queue).unwrap();
    let engines = WorkflowEngines::new(&ctx.router_config);
    ctx.workflow_engines.set(engines).unwrap();
    ctx
}

/// Runs the local worker registration workflow on `config`, as an AddWorker job does.
async fn register(ctx: &Arc<AppContext>, config: Value) -> Result<String, String> {
    let config: WorkerConfigRequest = serde_json::from_value(config).unwrap();
    let url = config.url.clone();
    let engine = &ctx.workflow_engines.get().unwrap().local_worker;
    let data = create_local_worker_workflow_data(config, ctx.clone());
    let id = engine
        .start_workflow(WorkflowId::new("local_worker_registration"), data)
        .await
        .unwrap();
    engine
        .wait_for_completion(id, &url, Duration::from_secs(60))
        .await
}

/// The prompt the gRPC pipeline renders for a user's "hi" once the tokenizer job
/// has registered `model`'s tokenizer.
async fn render(ctx: &AppContext, model: &str, kwargs: Value) -> String {
    let registered = async {
        while !ctx.tokenizer_registry.contains(model) {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    };
    tokio::time::timeout(Duration::from_secs(60), registered)
        .await
        .expect("the tokenizer job registers the model's tokenizer");
    let body = json!({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "chat_template_kwargs": kwargs,
    });
    let request: ChatCompletionRequest = serde_json::from_value(body).unwrap();
    let (_, resp) = prepare_chat(Arc::new(request), None, Some(model.into()), ctx).unwrap();
    resp.original_text.unwrap()
}

#[tokio::test]
async fn an_atom_worker_registers_as_atom_with_its_model_info_labels() {
    let info = model_info("atom-labels", "label: {{ messages[0].content }}", "{}");
    let url = serve_atom(info, 1).await;
    let ctx = context();
    register(&ctx, json!({ "url": url })).await.unwrap();

    let worker = ctx.worker_registry.get_by_url(&url).unwrap();
    assert_eq!(worker.metadata().runtime_type, RuntimeType::Atom);
    assert_eq!(worker.model_id(), "atom-labels");
    let labels = &worker.metadata().labels;
    for (key, value) in [
        ("model_path", "/models/atom-labels"),
        ("tokenizer_path", hf_tokenizer_path()),
        ("served_model_name", "atom-labels"),
        ("max_context_length", "4096"),
        ("eos_token_ids", "[2,7]"),
        ("chat_template", "label: {{ messages[0].content }}"),
        ("default_chat_template_kwargs_json", "{}"),
    ] {
        assert_eq!(labels.get(key).map(String::as_str), Some(value), "{key}");
    }
}

#[tokio::test]
async fn an_atom_worker_with_another_protocol_version_is_refused() {
    let url = serve_atom(model_info("atom-v2", "", "{}"), 2).await;
    let ctx = context();
    let error = register(&ctx, json!({ "url": url })).await.unwrap_err();

    assert!(error.contains("protocol version is 2"), "{error}");
    assert!(ctx.worker_registry.get_by_url(&url).is_none());
}

#[tokio::test]
async fn the_tokenizer_job_renders_with_the_label_template_without_a_flag() {
    let info = model_info("atom-template", "label: {{ messages[0].content }}", "{}");
    let url = serve_atom(info, 1).await;
    let ctx = context();
    register(&ctx, json!({ "url": url })).await.unwrap();

    // The fixture's own template would render "user: hi".
    let rendered = render(&ctx, "atom-template", Value::Null).await;
    assert_eq!(rendered, "label: hi");
}

#[tokio::test]
async fn an_explicit_chat_template_wins_over_the_label() {
    let dir = tempfile::TempDir::new().unwrap();
    let flag = dir.path().join("flag.jinja");
    std::fs::write(&flag, "flag: {{ messages[0].content }}").unwrap();
    let info = model_info("atom-flag", "label: {{ messages[0].content }}", "{}");
    let url = serve_atom(info, 1).await;
    let ctx = context();
    register(&ctx, json!({ "url": url, "chat_template": flag }))
        .await
        .unwrap();

    assert_eq!(render(&ctx, "atom-flag", Value::Null).await, "flag: hi");
}

#[tokio::test]
async fn default_template_kwargs_join_the_request_kwargs_the_request_winning() {
    let template = "{{ greeting }} {{ flag }}: {{ messages[0].content }}";
    let defaults = r#"{"greeting": "hello", "flag": "default"}"#;
    let url = serve_atom(model_info("atom-kwargs", template, defaults), 1).await;
    let ctx = context();
    register(&ctx, json!({ "url": url })).await.unwrap();

    let rendered = render(&ctx, "atom-kwargs", Value::Null).await;
    assert_eq!(rendered, "hello default: hi");
    let rendered = render(&ctx, "atom-kwargs", json!({ "flag": "request" })).await;
    assert_eq!(rendered, "hello request: hi");
}

#[tokio::test]
async fn a_vllm_worker_is_still_detected_as_vllm() {
    let service = vllm::vllm_engine_server::VllmEngineServer::new(FakeVllm);
    let url = serve(Server::builder().add_service(service)).await;
    let ctx = context();
    register(&ctx, json!({ "url": url })).await.unwrap();

    let worker = ctx.worker_registry.get_by_url(&url).unwrap();
    assert_eq!(worker.metadata().runtime_type, RuntimeType::Vllm);
    let labels = &worker.metadata().labels;
    assert_eq!(labels.get("max_context_length").unwrap(), "2048");
}

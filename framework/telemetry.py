"""ADOT SDK spans. No collector, prompts, tool inputs or results are exported."""
import contextlib
import contextvars
import functools
import os
import secrets
import threading
import time

_provider = None
_processor = None
_task = contextvars.ContextVar('cloudops_trace_task', default=None)
_lock = threading.RLock()


def identity():
    return f'{int(time.time()):08x}' + secrets.token_hex(12), secrets.token_hex(8)


def initialize():
    with _lock:
        return _initialize()


def _initialize():
    global _provider, _processor
    if _provider is not None or os.getenv('CLOUDOPS_TRACING_ENABLED') != 'true':
        return _provider
    os.environ.setdefault('AGENT_OBSERVABILITY_ENABLED', 'true')
    os.environ.setdefault('AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT', 'true')
    os.environ.setdefault('OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT', 'false')
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from amazon.opentelemetry.distro.exporter.otlp.aws.traces.otlp_aws_span_exporter import OTLPAwsSpanExporter
    import botocore.session
    region = os.getenv('AWS_REGION', 'cn-northwest-1')
    group = os.getenv('CLOUDOPS_TRACE_LOG_GROUP', 'aws/spans')
    exporter = OTLPAwsSpanExporter(aws_region=region, session=botocore.session.get_session(),
        endpoint=f'https://xray.{region}.amazonaws.com.cn/v1/traces',
        headers={'x-aws-log-group': group, 'x-aws-log-stream': 'spans'}, timeout=5)
    _provider = TracerProvider(resource=Resource.create({'service.name': 'cloudops-' + os.getenv('AGENT_NAME', 'control'),
        'aws.log.group.names': group}), shutdown_on_exit=True)
    class MetadataOnlyProcessor(BatchSpanProcessor):
        def on_end(self, span):
            if span.attributes and span.attributes.get('cloudops.task.id'):
                super().on_end(span)
    _processor = MetadataOnlyProcessor(exporter, schedule_delay_millis=1000, max_queue_size=2048)
    _provider.add_span_processor(_processor)
    trace.set_tracer_provider(_provider)
    return _provider


def parent(task):
    from opentelemetry import trace
    current = trace.get_current_span().get_span_context()
    if current.is_valid and f'{current.trace_id:032x}' == task.get('trace_id'):
        return None
    if not task.get('trace_id') or not task.get('root_span_id'):
        return None
    return trace.set_span_in_context(trace.NonRecordingSpan(trace.SpanContext(
        int(task['trace_id'], 16), int(task['root_span_id'], 16), True, trace.TraceFlags(1))))


def start(task, name, **attributes):
    if not initialize() or not task or not task.get('trace_id'):
        return None
    return _provider.get_tracer('cloudops').start_span(name, context=parent(task), attributes={
        'cloudops.task.id': task['id'], 'cloudops.root_task.id': task.get('root_task_id', task['id']),
        'cloudops.owner': task['owner'], **attributes})


@contextlib.contextmanager
def span(task, name, **attributes):
    s = start(task, name, **attributes)
    if s is None:
        yield
        return
    from opentelemetry import trace
    with trace.use_span(s, end_on_exit=True, record_exception=False, set_status_on_exception=False):
        try:
            yield s
        except Exception as exc:
            s.set_attribute('error.type', type(exc).__name__)
            s.set_status(trace.Status(trace.StatusCode.ERROR))
            raise


def traced(name, task_argument=0):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            task = (kwargs.get('task') or _task.get()) if task_argument is None else args[task_argument]
            token = _task.set(task)
            try:
                with span(task, name, **({'cloudops.phase': task.get('phase', 'execute'),
                                        'cloudops.attempt': task.get('attempts', 0)} if task else {})):
                    return fn(*args, **kwargs)
            finally:
                _task.reset(token)
        return wrapped
    return decorate


def complete(task):
    """Durable root includes queueing, parallel children and resumed worker stages."""
    if not initialize() or not task.get('trace_id') or task.get('parent_id'):
        return
    from opentelemetry import trace
    from opentelemetry.sdk.trace import ReadableSpan
    context = trace.SpanContext(int(task['trace_id'], 16), int(task['root_span_id'], 16), False, trace.TraceFlags(1))
    status = trace.Status(trace.StatusCode.ERROR if task['status']=='failed' else trace.StatusCode.OK)
    root = ReadableSpan(name='CloudOps 任务', context=context, resource=_provider.resource,
        attributes={'cloudops.task.id': task['id'], 'cloudops.owner': task['owner'], 'cloudops.status': task['status']},
        start_time=int(task['created_at']*1e9), end_time=int(task['finished_at']*1e9), status=status)
    _processor.on_end(root)

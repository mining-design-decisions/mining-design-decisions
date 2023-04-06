"""
Command line utility for managing and training
deep learning classifiers.
"""

##############################################################################
##############################################################################
# Imports
##############################################################################

import collections
import datetime
import json
import os.path
import pathlib
import getpass
import statistics
import typing
import threading
import warnings
import shlex
import traceback

import numpy

from . import classifiers, kw_analyzer, model_manager, model_io
from .classifiers import HyperParameter

from . import feature_generators
from .feature_generators import ParameterSpec
from .model_io import OutputMode
from . import data_manager
from . import embeddings

from . import learning
from .config import conf, CLIApp, APIApp
from .logger import get_logger
from .database import DatabaseAPI, parse_query
from . import metrics
log = get_logger('CLI')

from . import analysis
from . import prediction


##############################################################################
##############################################################################
# Parser Setup
##############################################################################


def main(args=None):
    conf.reset()
    app = build_app()
    log.info('Dispatching user command')
    app.parse_and_dispatch(args)


def invoke_pipeline_with_command(command: str) -> None | Exception:
    conf.reset()
    try:
        main(shlex.split(command))
    except Exception as e:
        return e


def invoke_pipeline_with_config(args: dict) -> None | Exception:
    conf.reset()
    app = build_app(api=True)
    log.info(f'Dispatching config: {args}')
    try:
        app.parse_and_dispatch(args)
    except Exception as e:
        return e


def get_arg_spec():
    location = os.path.split(__file__)[0]
    return os.path.join(location, 'cli.json')


def build_app(*, api=False):
    location = get_arg_spec()
    log.debug(f'Building CLI app from file {location}')
    if not api:
        app = CLIApp(location)
    else:
        app = APIApp(location)
    setup_app_constraints(app, api=api)
    return app


def setup_app_constraints(app, *, api=False):
    def add_eq_len_constraint(p, q):
        app.add_constraint(lambda x, y: len(x) == len(y),
                           'Argument lists must have equal length.',
                           p, q)

    def add_min_delta_constraints(cmd):
        app.add_constraint(lambda deltas, attrs: len(deltas) == len(attrs) or len(deltas) == 1,
                           'Requirement not satisfied: len(min-delta) = len(trimming-attributes) or len(min-delta) = 1',
                           f'{cmd}.min-delta', f'{cmd}.trimming-attribute')

    add_min_delta_constraints('run_analysis.summarize')
    add_min_delta_constraints('run_analysis.plot')
    add_min_delta_constraints('run_analysis.plot-attributes')
    add_min_delta_constraints('run_analysis.confusion')
    add_min_delta_constraints('run_analysis.compare')
    add_min_delta_constraints('run_analysis.compare-stats')

    add_eq_len_constraint('run.classifier', 'run.input_mode')
    add_eq_len_constraint('run.early-stopping-min-delta', 'run.early-stopping-attribute')

    app.add_constraint(lambda ensemble, test_sep: ensemble == 'none' or not test_sep,
                       'Cannot use ensemble when using separate testing mode.',
                       'run.ensemble-strategy', 'run.test-separately')
    app.add_constraint(lambda store, test_separately: not (store and test_separately),
                       'Cannot store model when using separate testing mode.',
                       'run.store-model', 'run.test-separately')
    app.add_constraint(lambda store, k: not (store and k > 0),
                       'Cannot store model when using k-fold cross validation',
                       'run.store-model', 'run.k-cross')
    app.add_constraint(lambda cross_project, k: k == 0 or not cross_project,
                       'Cannot use --k-cross and --cross-project at the same time.',
                       'run.cross-project', 'run.k-cross')
    app.add_constraint(
        lambda k, quick_cross: not quick_cross or k > 0,
        'Must specify k when running with --quick-cross',
        'run.k-cross', 'run.quick-cross'
    )
    app.add_constraint(
        lambda do_save, model_id: (not do_save) or (do_save and model_id),
        '--model-id must be given when storing a model.',
        'run.store-model', 'run.model-id'
    )
    app.add_constraint(
        lambda do_save, cache_features: (not do_save) or (do_save and not cache_features),
        'May not use --cache-features when using --store-model.',
        'run.store-model', 'run.cache-features'
    )
    app.add_constraint(
        lambda do_save, k, cross_project, quick_cross:
            (not do_save)
            or (k == 0
                and not cross_project
                and not quick_cross),
        'Cannot run cross validation (or cross study) scheme when saving a model.',
        'run.store-model',
        'run.k-cross',
        'run.cross-project',
        'run.quick-cross'
    )
    app.add_constraint(
        lambda do_analyze, _:
            not do_analyze or kw_analyzer.model_is_convolution(),
        'Can only analyze keywords when using a convolutional model',
        'run.analyze-keywords', 'run.classifier'
    )
    app.add_constraint(
        lambda do_analyze: (not do_analyze) or kw_analyzer.doing_one_run(),
        'Can not perform cross validation when extracting keywords',
        'run.analyze-keywords'
    )
    app.add_constraint(
        lambda test_query, test_with_train: (
            (not test_with_train) or test_query
        ),
        'Must either test with training data, or give a testing data query',
        'run.testing-data-query', 'run.test-with-training-data'
    )
    app.register_callback('predict', run_prediction_command)
    app.register_callback('run', run_classification_command)
    app.register_callback('list', run_list_command)
    app.register_callback('hyperparams', run_hyper_params_command)
    app.register_callback('generator-params', run_generator_params_command)
    app.register_callback('combination-strategies', show_combination_strategies)
    app.register_callback('run_analysis.summarize',
                          analysis.run_summarize_command)
    app.register_callback('run_analysis.plot-attributes',
                          analysis.run_plot_attributes_command)
    app.register_callback('run_analysis.plot',
                          analysis.run_bar_plot_command)
    app.register_callback('run_analysis.compare',
                          analysis.run_comparison_command)
    app.register_callback('run_analysis.confusion',
                          analysis.run_confusion_matrix_command)
    app.register_callback('run_analysis.compare-stats',
                          analysis.run_stat_command)
    app.register_callback('train', run_training_session)

    if not api:
        app.register_callback('serve', run_api)

    app.register_callback('generate-embedding', run_embedding_generation_command)
    app.register_callback('embedding-parameters', run_embedding_param_command)
    app.register_callback('embedding-generators', run_show_embeddings_command)
    app.register_callback('metrics', run_metrics_calculation_command)

    app.register_setup_callback(setup_peregrine)
    app.register_setup_callback(setup_storage)
    app.register_setup_callback(issue_warnings)
    app.register_setup_callback(setup_resources)
    app.register_setup_callback(setup_security)

    conf.register('system.app', object, app)
    conf.register('system.is-cli', bool, not api)

    log.debug('Finished building app')
    return app


def setup_peregrine():
    conf.clone('run.peregrine', 'system.peregrine')
    if conf.get('system.peregrine'):
        print('Running on Peregrine!')
        conf.register('system.peregrine.home', str, os.path.expanduser('~'))
        conf.register('system.peregrine.data', str, f'/data/{getpass.getuser()}')
        print(f'system.peregrine.home: {conf.get("system.peregrine.home")}')
        print(f'system.peregrine.data: {conf.get("system.peregrine.data")}')


def setup_storage():
    # Storage space and constants
    conf.register('system.storage.generators', list, [])
    conf.register('system.storage.auxiliary', list, [])
    conf.register('system.storage.auxiliary_map', dict, {})
    conf.register('system.storage.auxiliary_prefix', str, 'auxiliary')
    conf.register('system.storage.file_prefix', str, 'dl_pipeline')

    # Setup database
    if conf.is_active('run.database-url'):
        conf.clone('run.database-url', 'system.storage.database-url')
    if conf.is_active('train.database-url'):
        conf.clone('train.database-url', 'system.storage.database-url')
    if conf.is_active('predict.database-url'):
        conf.clone('predict.database-url', 'system.storage.database-url')
    if conf.is_active('generate-embedding.database-url'):
        conf.clone('generate-embedding.database-url', 'system.storage.database-url')
    if conf.is_active('metrics.database-url'):
        conf.clone('metrics.database-url', 'system.storage.database-url')
    if conf.is_registered('system.storage.database-url'):
        log.info(f'Registered database url: {conf.get("system.storage.database-url")}')
        conf.register('system.storage.database-api', DatabaseAPI, DatabaseAPI())    # Also invalidates the cache


def setup_resources():
    if conf.is_active('run.num-threads'):
        conf.clone('run.num-threads', 'system.resources.threads')
    if conf.is_active('train.num-threads'):
        conf.clone('train.num-threads', 'system.resources.threads')
    if conf.is_active('predict.num-threads'):
        conf.clone('predict.num-threads', 'system.resources.threads')
    if conf.is_active('generate-embedding.num-threads'):
        conf.clone('generate-embedding.num-threads', 'system.resources.threads')
    if conf.is_registered('system.resources.threads'):
        log.info(f'Available threads for preprocessing: {conf.get("system.resources.threads")}')


def setup_security():
    conf.register(
        'system.security.allow-self-signed',
        bool,
        os.environ.get('DL_MANAGER_ALLOW_SELF_SIGNED_CERTIFICATE', False)
    )
    conf.register(
        'system.security.certificate-authority',
        object,
        os.environ.get('DL_MANAGER_LOCAL_PUBLIC_KEY', True)
    )
    conf.set('system.security.certificate-authority', False)
    if conf.get('system.security.certificate-authority') is not True:
        if not conf.get('system.security.allow-self-signed'):
            raise ValueError('Cannot use self-signed certificates')

    # if not conf.get('system.is-cli'):
    #     if conf.is_active('train.keyfile'):
    #         conf.clone('train.keyfile', 'system.security.ssl-keyfile')
    #     if conf.is_active('train.certfile'):
    #         conf.clone('train.certfile', 'system.security.ssl-certfile')
    #     db: DatabaseAPI = conf.get('system.storage.database-api')
    #     conf.register('system.security.db-token',
    #                   object,
    #                   db.get_token(os.environ['DL_MANAGER_USERNAME'], os.environ['DL_MANAGER_PASSWORD']))
    # else:
    #     conf.register('system.security.db-token', object, None)
    log.info(f'Running active command: {conf.get("system.active-command")}')
    if conf.get('system.active-command') == 'serve':
        conf.clone('serve.keyfile', 'system.security.ssl-keyfile')
        conf.clone('serve.certfile', 'system.security.ssl-certfile')
        log.info(f'Key file: {conf.get("system.security.ssl-keyfile")}')
    if not conf.is_registered('system.security.db-token'):
        conf.register('system.security.db-token', object, None)


def issue_warnings():
    if conf.is_active('run.output-mode'):
        output_mode = OutputMode.from_string(conf.get('run.output-mode'))
        include_detection = conf.get('run.include-detection-performance')
        if output_mode == OutputMode.Detection and include_detection:
            warnings.warn('--include-detection-performance is ignored when doing classification')


def run_api():
    port = conf.get('serve.port')

    import uvicorn
    import fastapi

    app = fastapi.FastAPI()

    api_lock = threading.Lock()

    @app.post('/invoke')
    async def run_command(request: fastapi.Request, response: fastapi.Response):
        if not api_lock.acquire(blocking=False):
            response.status_code = 503  # Unavailable
            return {'error': 'dl_manager is currently busy executing another command'}
        payload = await request.json()
        conf.set('system.security.db-token', payload['auth']['token'])
        try:
            params = payload['config']
            log.info('Running pipeline with params', params)
            #result = invoke_pipeline_with_config(params)
            #web_app: APIApp = build_app(api=True)
            cfg = {
                'system.security.ssl-keyfile': (str, conf.get('system.security.ssl-keyfile')),
                'system.security.ssl-certfile': (str, conf.get('system.security.ssl-certfile')),
                'system.security.db-token': (object, conf.get('system.security.db-token'))
            }
            try:
                is_training = params['subcommand_name_0'] in {'train', 'run'}
            except KeyError:
                raise ValueError('parameter `subcommand_name_0` not given')
            to_retrieve = []
            if is_training:
                to_retrieve.append('system.training-start-time')
            if params['subcommand_name_0'] == 'metrics':
                to_retrieve.append('system.metrics.results')
            try:
                copied = APIApp.execute_session(
                    get_arg_spec(),
                    params,
                    app_initializer=setup_app_constraints,
                    with_config=cfg,
                    retrieve_configs=to_retrieve
                )
            except Exception as e:
                log.warning(f'An exception occurred during user command: {e}')
                log.warning(' '.join(traceback.format_exception(e)))
                copied = None   # Shut up, PyCharm
                result = e
            else:
                result = None
            conf.set('system.security.db-token', None)
            if result is None:
                if is_training:
                    return {'run-id': copied['system.training-start-time']}
                elif params['subcommand_name_0'] == 'metrics':
                    return copied['system.metrics.results']
                return {}
            return {'error': str(result)}
        finally:
            api_lock.release()

    uvicorn.run(
        app,
        host='0.0.0.0',
        port=port,
        ssl_keyfile=conf.get('system.security.ssl-keyfile'),
        ssl_certfile=conf.get('system.security.ssl-certfile')
    )


def run_training_session():
    config_id = conf.get('train.model-id')
    db: DatabaseAPI = conf.get('system.storage.database-api')
    settings = db.get_model_config(config_id)
    settings |= {
        'subcommand_name_0': 'run',
        'num-threads': conf.get('system.resources.threads'),
        'database-url': conf.get('system.storage.database-url'),
        'model-id': conf.get('train.model-id')
    }
    cfg = {
        'system.security.ssl-keyfile': (str, conf.get('system.security.ssl-keyfile')),
        'system.security.ssl-certfile': (str, conf.get('system.security.ssl-certfile')),
        'system.security.db-token': (object, conf.get('system.security.db-token'))
    }
    state = APIApp.execute_session(
        get_arg_spec(),
        settings,
        app_initializer=setup_app_constraints,
        retrieve_configs=['system.training-start-time'],
        with_config=cfg
    )
    conf.register(
        'system.training-start-time',
        str,
        state['system.training-start-time']
    )


##############################################################################
##############################################################################
# Command Dispatch - Combination Strategies
##############################################################################

STRATEGIES = {
    'add': 'Add the values of layers to combine them.',
    'subtract': 'Subtract values of layers to combine them. Order matters',
    'multiply': 'Multiply the values of layers to combine them.',
    'max': 'Combine two inputs or layers by taking the maximum.',
    'min': 'Combine two inputs or layers by taking the minimum.',
    'dot': 'Combine two inputs or layers by computing their dot product.',
    'concat': 'Combine two inputs or layers by combining them into one single large layer.',
    'boosting': 'Train a strong classifier using boosting. Only a single model must be given.',
    'stacking': 'Train a strong classifier using stacking. Ignores the simple combination strategy.',
    'voting': 'Train a strong classifier using voting. Ignores the simple combination strategy.'
}


def show_combination_strategies():
    margin = max(map(len, STRATEGIES))
    for strategy in sorted(STRATEGIES):
        print(f'{strategy.rjust(margin)}: {STRATEGIES[strategy]}')


##############################################################################
##############################################################################
# Command Dispatch - list command
##############################################################################


def run_list_command():
    match conf.get('list.arg'):
        case 'classifiers':
            _show_classifier_list()
        case 'inputs':
            _show_input_mode_list()
        case 'outputs':
            _show_enum_list('Output Mode', OutputMode)


def _show_classifier_list():
    print(f'Available Classifiers:')
    _print_keys(list(classifiers.models))


def _show_input_mode_list():
    print(f'Available Input Modes:')
    _print_keys(list(feature_generators.generators))


def _show_enum_list(name: str, obj):
    print(f'Possible values for {name} setting:')
    keys = [key for key in vars(obj) if not key.startswith('_') and key[0].isupper()]
    _print_keys(keys)


def _print_keys(keys):
    keys.sort()
    for key in keys:
        print(f'\t* {key}')


##############################################################################
##############################################################################
# Command Dispatch - hyperparams command
##############################################################################


def run_hyper_params_command():
    classifier = conf.get('hyperparams.classifier')
    if classifier not in classifiers.models:
        return print(f'Unknown classifier: {classifier}')
    cls = classifiers.models[classifier]
    keys = []
    name: str
    param: HyperParameter
    for name, param in cls.get_hyper_parameters().items():
        keys.append((f'{name} -- '
                     f'[min, max] = [{param.minimum}, {param.maximum}] -- '
                     f'default = {param.default}'))
    print(f'Hyper-parameters for {classifier}:')
    _print_keys(keys)


##############################################################################
##############################################################################
# Command Dispatch - generator-params command
##############################################################################


def run_generator_params_command():
    generator = conf.get('generator-params.generator')
    if generator not in feature_generators.generators:
        return print(f'Unknown feature generator: {generator}')
    cls = feature_generators.generators[generator]
    keys = []
    name: str
    param: ParameterSpec
    for name, param in cls.get_parameters().items():
        keys.append(f'{name} -- {param.description}')
    print(f'Parameters for {generator}:')
    _print_keys(keys)


##############################################################################
##############################################################################
# Command Dispatch - embedding command
#############################################################################

def run_show_embeddings_command():
    print(','.join(embeddings.generators.keys()))

##############################################################################
##############################################################################
# Command Dispatch - embedding params  command
#############################################################################


def run_embedding_param_command():
    generator: typing.Type[embeddings.AbstractEmbeddingGenerator]
    generator = conf.get('embedding-parameters.generator')
    if generator not in embeddings.generators:
        return print(f'Unknown embedding generator: {generator}')
    cls = embeddings.generators[generator]
    keys = []
    name: str
    param: embeddings.EmbeddingGeneratorParam
    for name, param in cls.get_params().items():
        keys.append(f'{name} -- {param.description}')
    print(f'Parameters for {generator}:')
    _print_keys(keys)

##############################################################################
##############################################################################
# Command Dispatch - Embedding Generation
#############################################################################


def run_embedding_generation_command():
    gen = conf.get('generate-embedding.generator')
    generator: typing.Type[embeddings.AbstractEmbeddingGenerator] = embeddings.generators[gen]
    query = conf.get('generate-embedding.training-data-query')
    path = conf.get('generate-embedding.target-file')
    handling = conf.get('generate-embedding.formatting-handling')
    g = generator(**conf.get('generate-embedding.params'))
    g.make_embedding(query, path, handling)


##############################################################################
##############################################################################
# Feature Generation
##############################################################################


def generate_features_and_get_data(architectural_only: bool = False,
                                   force_regenerate: bool = False):
    input_mode = conf.get('run.input_mode')
    output_mode = conf.get('run.output_mode')
    params = conf.get('run.params')
    imode_counts = collections.defaultdict(int)
    datasets_train = []
    labels_train = None
    binary_labels_train = None
    datasets_test = []
    labels_test = None
    binary_labels_test = None
    for imode in input_mode:
        number = imode_counts[imode]
        imode_counts[imode] += 1
        # Get the parameters for the feature generator
        mode_params = _normalize_param_names(
            params.get(imode, {}) |
            params.get('default', {}) |
            params.get(f'{imode}[{number}]', {})
        )
        # Validate that the parameters are valid
        valid_params = feature_generators.generators[imode].get_parameters()
        for param_name in mode_params:
            if param_name not in valid_params:
                raise ValueError(f'Invalid parameter for feature generator {imode}: {param_name}')
        training_query = json.dumps(
            {
                '$and': [
                    {'tags': {'$eq': 'has-label'}},
                    {'tags': {'$ne': 'needs-review'}},
                    parse_query(conf.get('run.training-data-query'))
                ]
            }
        )
        generator = feature_generators.generators[imode](**mode_params)
        dataset = generator.generate_features(training_query, output_mode)
        if labels_train is not None:
            assert labels_train == dataset.labels
            assert binary_labels_train == dataset.binary_labels
        else:
            labels_train = dataset.labels
            binary_labels_train = dataset.binary_labels
        datasets_train.append(dataset)
        if not conf.get('run.test-with-training-data'):
            testing_query = json.dumps(
                {
                    '$and': [
                        {'tags': {'$eq': 'has-label'}},
                        {'tags': {'$ne': 'needs-review'}},
                        parse_query(conf.get('run.test-data-query'))
                    ]
                }
            )
            generator = feature_generators.generators[imode](**mode_params)
            dataset = generator.generate_features(testing_query, output_mode)
            if labels_test is not None:
                assert labels_test == dataset.labels
                assert binary_labels_test == dataset.binary_labels
            else:
                labels_test = dataset.labels
                binary_labels_test = dataset.binary_labels
            datasets_test.append(dataset)

    if architectural_only:
        datasets_train, labels_train = select_architectural_only(datasets_train,
                                                                 labels_train,
                                                                 binary_labels_train)
        datasets_test, labels_test = select_architectural_only(datasets_test,
                                                               labels_test,
                                                               binary_labels_test)

    return (
        (datasets_train, labels_train),
        (datasets_test, labels_test)
    )


def select_architectural_only(datasets, labels, binary_labels):
    new_features = [[] for _ in range(len(datasets))]
    for index, is_architectural in enumerate(binary_labels):
        if is_architectural:
            for j, dataset in enumerate(datasets):
                new_features[j].append(dataset.features[index])
    new_datasets = []
    for old_dataset, new_feature_list in zip(datasets, new_features):
        new_dataset = data_manager.Dataset(
            features=new_feature_list,
            labels=[label for bin_label, label in zip(binary_labels, labels) if bin_label],
            shape=old_dataset.shape,
            embedding_weights=old_dataset.embedding_weights,
            vocab_size=old_dataset.vocab_size,
            weight_vector_length=old_dataset.weight_vector_length,
            binary_labels=old_dataset.binary_labels,
            issue_keys=old_dataset.issue_keys,
            ids=old_dataset.ids
        )
        new_datasets.append(new_dataset)
    #datasets = new_datasets
    #labels = datasets[0].labels
    return new_datasets, new_datasets[0].labels


##############################################################################
##############################################################################
# Command Dispatch - run command
##############################################################################


def run_classification_command():
    conf.register('system.training-start-time', str, datetime.datetime.utcnow().isoformat())

    classifier = conf.get('run.classifier')
    input_mode = conf.get('run.input_mode')
    output_mode = conf.get('run.output_mode')
    params = conf.get('run.params')
    epochs = conf.get('run.epochs')
    k_cross = conf.get('run.k_cross')
    regenerate_data = not conf.get('run.cache-features')
    architectural_only = conf.get('run.architectural_only')
    hyper_parameters = conf.get('run.hyper-params')

    datasets_train, labels_train, datasets_test, labels_test, factory = _get_model_factory(
        input_mode, output_mode, params, hyper_parameters,
        architectural_only, regenerate_data, classifier
    )

    training_data = (
        [ds.features for ds in datasets_train],
        labels_train,
        datasets_train[0].issue_keys
    )
    if datasets_test:
        testing_data = (
            [ds.features for ds in datasets_test],
            labels_test,
            datasets_test[0].issue_keys
        )
    else:
        testing_data = None

    if conf.get('run.ensemble-strategy') != 'none':
        learning.run_ensemble(factory,
                              training_data,
                              testing_data,
                              OutputMode.from_string(output_mode).label_encoding)
        log.info(f'Model ID: {conf.get("system.training-start-time")}')
        return

    # 5) Invoke actual DL process
    if k_cross == 0 and not conf.get('run.cross-project'):
        learning.run_single(factory(),
                            epochs,
                            OutputMode.from_string(output_mode),
                            OutputMode.from_string(output_mode).label_encoding,
                            training_data,
                            testing_data)
    else:
        learning.run_cross(factory,
                           epochs,
                           OutputMode.from_string(output_mode),
                           OutputMode.from_string(output_mode).label_encoding,
                           training_data,
                           testing_data)
    log.info(f'Model ID: {conf.get("system.training-start-time")}')


def _get_model_factory(input_mode,
                       output_mode,
                       params,
                       hyper_parameters,
                       architectural_only,
                       regenerate_data,
                       classifier):
    ((datasets, labels), (datasets_test, labels_test)) = generate_features_and_get_data(
        architectural_only, regenerate_data
    )

    # 3) Define model factory

    def factory():
        models = []
        keras_models = []
        output_encoding = OutputMode.from_string(output_mode).output_encoding
        output_size = OutputMode.from_string(output_mode).output_size
        stream = zip(classifier, input_mode, datasets)
        model_counts = collections.defaultdict(int)
        for name, mode, data in stream:
            try:
                generator = feature_generators.generators[mode]
            except KeyError:
                raise ValueError(f'Unknown input mode: {mode}')
            input_encoding = generator.input_encoding_type()
            try:
                model_factory = classifiers.models[name]
            except KeyError:
                raise ValueError(f'Unknown classifier: {name}')
            if input_encoding not in model_factory.supported_input_encodings():
                raise ValueError(
                    f'Input encoding {input_encoding} not compatible with model {name}'
                )
            model: classifiers.AbstractModel = model_factory(data.shape,
                                                             input_encoding,
                                                             output_size,
                                                             output_encoding)
            models.append(model)
            model_number = model_counts[name]
            model_counts[name] += 1
            hyperparams = _normalize_param_names(
                hyper_parameters.get(name, {}) |
                hyper_parameters.get(f'{name}[{model_number}]', {}) |
                hyper_parameters.get('default', {})
            )
            allowed_hyper_params = model.get_hyper_parameters()
            for param_name in hyperparams:
                if param_name not in allowed_hyper_params:
                    raise ValueError(f'Illegal hyperparameter for model {name}: {param_name}')
            if data.is_embedding():
                keras_model = model.get_compiled_model(embedding=data.embedding_weights,
                                                       embedding_size=data.vocab_size,
                                                       embedding_output_size=data.weight_vector_length,
                                                       **hyperparams)
            else:
                keras_model = model.get_compiled_model(**hyperparams)
            keras_models.append(keras_model)
        # 4) If necessary, combine models
        if len(models) == 1:
            final_model = keras_models[0]
        elif conf.get('run.ensemble_strategy') not in ('stacking', 'voting') and not conf.get('run.test-separately'):
            final_model = classifiers.combine_models(
                models[0], *keras_models, fully_connected_layers=(None, None)
            )
        else:
            return keras_models  # Return all models separately, required for stacking or separate testing
        final_model.summary()
        return final_model

    return datasets, labels, datasets_test, labels_test, factory


def _normalize_param_names(params):
    return {key.replace('_', '-'): value for key, value in params.items()}


##############################################################################
##############################################################################
# Command Dispatch - Prediction Command
##############################################################################


def run_prediction_command():
    # Step 1: Load model data
    data_query = conf.get('predict.data-query')
    model_id: str = conf.get('predict.model')
    model_version = conf.get('predict.version')
    db: DatabaseAPI = conf.get('system.storage.database-api')
    if model_version == 'most-recent':
        model_version = db.get_most_recent_model(model_id)
    model_manager.load_model_from_zip(db.retrieve_model(model_id, model_version))
    model = pathlib.Path(model_manager.MODEL_DIR)
    with open(model / 'model.json') as file:
        model_metadata = json.load(file)
    output_mode = OutputMode.from_string(
        model_metadata['model_settings']['run.output_mode']
    )

    # Step 2: Load data
    datasets = []
    warnings.warn('The predict command does not cache features!')
    auxiliary_files = {
        file: os.path.join(model, path)
        for file, path in model_metadata['auxiliary_files'].items()
    }
    conf.get('system.storage.auxiliary_map').update(auxiliary_files)
    ids = None
    for generator in model_metadata['feature_generators']:
        with open(model / generator) as file:
            generator_data = json.load(file)
        prefix = conf.get('system.storage.file-prefix')
        feature_file = pathlib.Path(f'{prefix}_prediction_features.json')
        generator_class = feature_generators.generators[generator_data['generator']]
        generator = generator_class(
            pretrained_generator_settings=generator_data['settings']
        )
        data_stuff = generator.generate_features(data_query, output_mode.name)
        if ids is None:
            ids = data_stuff.ids
        if type(data_stuff.features) is dict:
            datasets.append(data_stuff.features)
        else:
            datasets.append(numpy.asarray(data_stuff.features))

    # Step 3: Load the model and get the predictions
    match model_metadata['model_type']:
        case 'single':
            prediction.predict_simple_model(model,
                                            model_metadata,
                                            datasets,
                                            output_mode,
                                            ids,
                                            model_id,
                                            model_version)
        case 'stacking':
            prediction.predict_stacking_model(model,
                                              model_metadata,
                                              datasets,
                                              output_mode,
                                              ids,
                                              model_id,
                                              model_version)
        case 'voting':
            prediction.predict_voting_model(model,
                                            model_metadata,
                                            datasets,
                                            output_mode,
                                            ids,
                                            model_id,
                                            model_version)
        case _ as tp:
            raise ValueError(f'Invalid model type: {tp}')


##############################################################################
##############################################################################
# Command Dispatch - Metric Calculation
##############################################################################


def run_metrics_calculation_command():
    db: DatabaseAPI = conf.get('system.storage.database-api')
    model_id = conf.get('metrics.model-id')
    model_config = db.get_model_config(model_id)
    version_id = conf.get('metrics.version-id')
    metric_settings = conf.get('metrics.metrics')
    if isinstance(metric_settings, str):
        metric_settings = json.loads(metric_settings.replace("'", '"'))
    results = db.load_training_results(model_id, version_id)
    results_per_fold = []
    for fold in results:
        match conf.get('metrics.epoch'):
            case 'last':
                epoch = -1
                results_per_fold.append([
                    _calculate_metrics(metric_settings, fold, epoch, model_config)
                ])
            case 'stopping-point':
                es_settings = metric_settings['early_stopping_settings']
                if es_settings['use_early_stopping']:
                    if es_settings['stopped_early']:
                        epoch = -1 - es_settings['patience']
                    else:
                        epoch = -1
                else:
                    epoch = -1
                results_per_fold.append([
                    _calculate_metrics(metric_settings, fold, epoch, model_config)
                ])
            case 'all':
                results_per_fold.append(
                    [
                        _calculate_metrics(metric_settings, fold, e, model_config)
                        for e in range(len(results['predictions']['training']))
                    ]
                )
            case _ as x:
                epoch = int(x)
                results_per_fold.append([
                    _calculate_metrics(metric_settings, fold, epoch, model_config)
                ])
    # results_per_fold has the following structure:
    #   [ fold1, fold2, ..., foldn]
    # where foldi is list of metrics per epoch.
    result = {
        'folds': results_per_fold,
        'aggregated': _compute_aggregate_metrics(metric_settings, results_per_fold)
    }
    conf.register('system.metrics.results', object, result)


def _compute_aggregate_metrics(metric_settings, results_per_fold):
    result = {'training': {}, 'validation': {}, 'testing': {}}
    for metric in metric_settings:
        mode = metric['dataset']
        metric_name = metric['metric']
        variant = metric['variant']
        key = f'{metric_name}[{variant}]'
        result[mode][key] = {}
        result[mode][key]['average'] = [
            statistics.mean([v[mode][key] for v in values])
            for values in zip(*results_per_fold)
        ]
        if len(results_per_fold) == 1:
            result[mode][key]['standard_deviation'] = [None] * len(result[mode][key]['average'])
        else:
            result[mode][key]['standard_deviation'] = [
                statistics.stdev([v[key] for v in values])
                for values in zip(*results_per_fold)
            ]
    return result


def _calculate_metrics(metric_settings, results, epoch, model_config):
    training_manager = metrics.MetricCalculationManager(
        y_true=numpy.asarray(results['truth']['training']),
        y_pred=numpy.asarray(results['predictions']['training'][epoch]),
        output_mode=OutputMode.from_string(
            model_config['output_mode'] if 'output_mode' in model_config else model_config['output-mode']
        ),
        classification_as_detection=conf.get('metrics.classification-as-detection'),
        include_non_arch=conf.get('metrics.include-non-arch')
    )
    validation_manager = metrics.MetricCalculationManager(
        y_true=numpy.asarray(results['truth']['validation']),
        y_pred=numpy.asarray(results['predictions']['validation'][epoch]),
        output_mode=OutputMode.from_string(
            model_config['output_mode'] if 'output_mode' in model_config else model_config['output-mode']
        ),
        classification_as_detection=conf.get('metrics.classification-as-detection'),
        include_non_arch=conf.get('metrics.include-non-arch')
    )
    testing_manager = metrics.MetricCalculationManager(
        y_true=numpy.asarray(results['truth']['testing']),
        y_pred=numpy.asarray(results['predictions']['testing'][epoch]),
        output_mode=OutputMode.from_string(
            model_config['output_mode'] if 'output_mode' in model_config else model_config['output-mode']
        ),
        classification_as_detection=conf.get('metrics.classification-as-detection'),
        include_non_arch=conf.get('metrics.include-non-arch')
    )
    managers = {
        'training': training_manager,
        'validation': validation_manager,
        'testing': testing_manager
    }
    result = {'training': {}, 'validation': {}, 'testing': {}}
    for metric in metric_settings:
        mode = metric['dataset']
        metric_name = metric['metric']
        variant = metric['variant']
        if mode not in result:
            raise ValueError(f'Invalid mode: {mode}')
        if metric_name != 'loss':
            result[mode][f'{metric_name}[{variant}]'] = managers[mode].calc_metric(metric_name, variant)
        else:
            result[mode][f'loss[{variant}]'] = results['loss'][mode][epoch]
    return result

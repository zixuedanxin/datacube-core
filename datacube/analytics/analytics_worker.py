"""Analytics Engine Celery worker.
This worker will be cloned in the AE cluster.
Currently JRO service calls e.g. updates are handled by this worker.
If performance is not as responsive as requried, a new cluster for
JRO service calls will be created.
"""

from __future__ import absolute_import, print_function

from sys import modules
from celery import Celery

from .analytics_engine2 import AnalyticsEngineV2
from .update_engine2 import UpdateEngineV2
from .base_job_monitor import BaseJobMonitor
from datacube.execution.execution_engine2 import ExecutionEngineV2
from datacube.config import LocalConfig


# Engines need to be declared globally because of celery
# pylint: disable=invalid-name
# analytics_engine = None
# pylint: disable=invalid-name
# execution_engine = None
# pylint: disable=invalid-name
# update_engine = None
# pylint: disable=invalid-name
config = None


def celery_app(store_config=None):
    try:
        if store_config is None:
            local_config = LocalConfig.find()
            store_config = local_config.redis_celery_config
        _app = Celery('ee_task', broker=store_config['url'], backend=store_config['url'])
    except ValueError:
        _app = Celery('ee_task')

    _app.conf.update(
        task_serializer='pickle',
        result_serializer='pickle',
        accept_content=['pickle'],
        worker_prefetch_multiplier=1)
    return _app


# def initialise_engines(config=None):
#     # pylint: disable=global-statement
#     global analytics_engine, execution_engine, update_engine
#     analytics_engine = AnalyticsEngineV2(config)
#     execution_engine = ExecutionEngineV2(config)
#     update_engine = UpdateEngineV2(config)

# pylint: disable=invalid-name
app1 = celery_app()

# TODO: In production environment, the engines need to be started using a local config identified
# through `find()`. This is not desirable in pytest as it will use the default config which is
# invalid and crashes all the tests. For now, we simply check whether this is run within
# pytest. This must be addressed another way.
# if 'pytest' not in modules:
# initialise_engines()


def launch_ae_worker(local_config):
    """Only used for pytests"""
    if not local_config:
        local_config = LocalConfig.find()
    global config
    config = local_config
    store_config = local_config.redis_celery_config
    # initialise_engines(local_config)
    from multiprocessing import Process
    process = Process(target=launch_worker_thread, args=(store_config['url'],))
    process.start()
    return process


def launch_worker_thread(url):
    """Only used for pytests"""
    app1.conf.update(result_backend=url,
                    broker_url=url)
    argv = ['worker', '-A', 'datacube.analytics.analytics_worker', '-l', 'INFO', '--autoscale=3,0']
    app1.worker_main(argv)


def stop_worker():
    """Only used for pytests"""
    app1.control.shutdown()


@app1.task
def run_python_function_base(function, function_params=None, data=None, user_tasks=None,
                             walltime=None, paths=None, env=None, output_dir=None,
                             *args, **kwargs):
    '''Process the function and data submitted by the user.'''
    analytics_engine = AnalyticsEngineV2('Analytics Engine', paths, env, output_dir)
    if not analytics_engine:
        raise RuntimeError('Analytics engine must be initialised by calling `initialise_engines`')
    jro, decomposed = analytics_engine.analyse(function, function_params, data, user_tasks, *args, **kwargs)

    subjob_tasks = []
    for job in decomposed['jobs']:
        subjob_tasks.append(run_python_function_subjob.delay(job, jro[0]['id'], paths, env, output_dir,
                                                             *args, **kwargs))

    monitor_task = monitor_jobs.delay(decomposed, subjob_tasks, walltime, paths, env, output_dir)

    return jro


@app1.task
def run_python_function_subjob(job, base_job_id, paths=None, env=None, output_dir=None,
                               *args, **kwargs):
    '''Process a subjob, created by the base job.'''
    execution_engine = ExecutionEngineV2('Execution Engine', paths, env, output_dir)
    if not execution_engine:
        raise RuntimeError('Execution engine must be initialised by calling `initialise_engines`')
    execution_engine.execute(job, base_job_id, *args, **kwargs)


@app1.task
def monitor_jobs(decomposed, subjob_tasks, walltime, paths=None, env=None, output_dir=None):
    '''Monitors base job.'''
    base_job_monitor = BaseJobMonitor('Base Job Monitor', decomposed, subjob_tasks, walltime, paths, env, output_dir)
    base_job_monitor.monitor_completion()


@app1.task
def get_update(action, item_id, paths=None, env=None):
    '''Return an update on a job or result.'''
    update_engine = UpdateEngineV2(paths, env)
    if not update_engine:
        raise RuntimeError('Update engine must be initialised by calling `initialise_engines`')
    result = update_engine.execute(action, item_id)
    return result
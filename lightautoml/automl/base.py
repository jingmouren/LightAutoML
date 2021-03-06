"""Base AutoML class."""

from typing import Sequence, Any, Optional, Iterable, Dict, List

from log_calls import record_history

from .blend import Blender, BestModelSelector
from ..dataset.base import LAMLDataset
from ..dataset.utils import concatenate
from ..pipelines.ml.base import MLPipeline
from ..reader.base import Reader
from ..utils.logging import get_logger, verbosity_to_loglevel
from ..utils.timer import PipelineTimer
from ..validation.utils import create_validation_iterator

logger = get_logger(__name__)


@record_history(enabled=False)
class AutoML:
    """Class for compile full pipeline of AutoML task.

    AutoML steps:

        - read, analyze data and get inner LAMLDataset from input dataset: performed by reader
        - create validation scheme
        - compute passed ml pipelines from levels. Each element of levels is list of MLPipelines
          prediction from current level are passed to next level pipelines as features
        - time monitoring - check if we have enough time to calc new pipeline
        - blend last level models and prune useless pipelines to speedup inference: performed by blender
        - returns prediction on validation data. If crossvalidation scheme is used, out-of-fold prediction will returned
          If validation data is passed - will return prediction on validation dataset
          In case of cv scheme when some point of train data never was used as validation (ex. timeout exceeded
          or custom cv iterator like TimeSeriesIterator was used) NaN for this point will be returned

    Example:
        Common usecase - create custom pipelines or presets.

        >>> reader = SomeReader()
        >>> pipe = MLPipeline([SomeAlgo()])
        >>> levels = [[pipe]]
        >>> automl = AutoML(reader, levels, )
        >>> automl.fit_predict(data, roles={'target: 'TARGET'})

    """

    def __init__(self, reader: Reader, levels: Sequence[Sequence[MLPipeline]], timer: Optional[PipelineTimer] = None,
                 blender: Optional[Blender] = None, skip_conn: bool = False, verbose: int = 2):
        """

        Args:
            reader: instance of Reader class - object that creates LAMLDataset from input data.
            levels: list of list of MLPipelines.
            timer: instance of PipelineTimer. Default - unlimited timer.
            blender: instance of Blender. By default - BestModelSelector.
            skip_conn: True if we should pass first level input features to next levels.
            verbose: verbosity level. Levels:
                - 0 - no messages.
                - 1 - warnings.
                - 2 - info.
                - 3 - debug.

        """
        self._initialize(reader, levels, timer, blender, skip_conn, verbose)

    def _initialize(self, reader: Reader, levels: Sequence[Sequence[MLPipeline]], timer: Optional[PipelineTimer] = None,
                    blender: Optional[Blender] = None, skip_conn: bool = False, verbose: int = 2):
        """Same as __init__. Exists for delayed initialization in presets.

        Args:
            reader: instance of Reader class - object that creates LAMLDataset from input data.
            levels: list of list of MLPipelines.
            timer: instance of PipelineTimer. Default - unlimited timer.
            blender: instance of Blender. By default - BestModelSelector.
            skip_conn: True if we should pass first level input features to next levels.
            verbose: verbosity level. Default 2.

        """

        logger.setLevel(verbosity_to_loglevel(verbose))
        assert len(levels) > 0, 'At least 1 level should be defined'

        self.timer = timer
        if timer is None:
            self.timer = PipelineTimer()
        self.reader = reader
        self._levels = levels

        # default blender is - select best model and prune other pipes
        self.blender = blender
        if blender is None:
            self.blender = BestModelSelector()

        # update model names
        for i, lvl in enumerate(self._levels):

            for j, pipe in enumerate(lvl):
                pipe.upd_model_names('Lvl_{0}_Pipe_{1}'.format(i, j))

        self.skip_conn = skip_conn

    def fit_predict(self, train_data: Any, roles: dict, train_features: Optional[Sequence[str]] = None,
                    cv_iter: Optional[Iterable] = None,
                    valid_data: Optional[Any] = None,
                    valid_features: Optional[Sequence[str]] = None) -> LAMLDataset:
        """Fit on input data and make prediction on validation part.

        Args:
            train_data: Dataset to train.
            roles: Roles dict.
            train_features: Optional features names, if cannot be inferred from train_data.
            cv_iter: Custom cv iterator. Ex. `TimeSeriesIterator` instance.
            valid_data: Optional validation dataset.
            valid_features: Optional validation dataset features if cannot be inferred from valid_data.

        Returns:
            Predicted values.

        """
        self.timer.start()
        train_dataset = self.reader.fit_read(train_data, train_features, roles)

        assert len(self._levels) <= 1 or train_dataset.folds is not None, \
            'Not possible to fit more than 1 level without cv folds'

        assert len(self._levels) <= 1 or valid_data is None, \
            'Not possible to fit more than 1 level with holdout validation'

        valid_dataset = None
        if valid_data is not None:
            valid_dataset = self.reader.read(valid_data, valid_features, add_array_attrs=True)

        train_valid = create_validation_iterator(train_dataset, valid_dataset, n_folds=None, cv_iter=cv_iter)
        # for pycharm)
        level_predictions = None
        pipes = None

        self.levels = []

        for n, level in enumerate(self._levels, 1):

            logger.info('\n')
            logger.info('Layer {} ...'.format(n))

            pipes = []
            level_predictions = []
            flg_last_level = n == len(self._levels)

            logger.info('Train process start. Time left {0} secs'.format(self.timer.time_left))

            for k, ml_pipe in enumerate(level):

                pipe_pred = ml_pipe.fit_predict(train_valid)
                level_predictions.append(pipe_pred)
                pipes.append(ml_pipe)

                logger.info('Time left {0}'.format(self.timer.time_left))

                if self.timer.time_limit_exceeded():
                    logger.warning('Time limit exceeded. Last level models will be blended and unused pipelines will be pruned. \
                                        \nTry to set higher time limits or use Profiler to find bottleneck and optimize Pipelines settings')

                    flg_last_level = True
                    break
            else:
                if self.timer.child_out_of_time:
                    logger.warning('Time limit exceeded in one of the tasks. AutoML will blend level {0} models. \
                                        \nTry to set higher time limits or use Profiler to find bottleneck and optimize Pipelines settings'
                                   .format(n)
                                   )
                    flg_last_level = True

            # here is split on exit condition
            if not flg_last_level:

                self.levels.append(pipes)
                level_predictions = concatenate(level_predictions)

                if self.skip_conn:
                    valid_part = train_valid.get_validation_data()
                    try:
                        # convert to initital dataset type
                        level_predictions = valid_part.from_dataset(level_predictions)
                    except TypeError:
                        raise TypeError('Can not convert prediction dataset type to input features. Set skip_conn=False')
                    level_predictions = concatenate([level_predictions, valid_part])
                train_valid = create_validation_iterator(level_predictions, None, n_folds=None, cv_iter=None)
            else:
                break

            logger.info('Layer {} training completed.'.format(n))

        blended_prediction, last_pipes = self.blender.fit_predict(level_predictions, pipes)
        self.levels.append(last_pipes)

        self.reader.upd_used_features(remove=list(set(self.reader.used_features) - set(self.collect_used_feats())))

        del self._levels
        return blended_prediction

    def predict(self, data: Any, features_names: Optional[Sequence[str]] = None) -> LAMLDataset:
        """Predict with automl on new dataset.

        Args:
            data: Dataset to perform inference.
            features_names: Optional features names, if cannot be inferred from train_data.

        Returns:
            Dataset with predictions.

        """
        dataset = self.reader.read(data, features_names=features_names, add_array_attrs=False)

        # for pycharm)
        blended_prediction = None

        for n, level in enumerate(self.levels, 1):
            # check if last level

            level_predictions = []
            for _n, ml_pipe in enumerate(level):
                level_predictions.append(ml_pipe.predict(dataset))

            if n != len(self.levels):

                level_predictions = concatenate(level_predictions)

                if self.skip_conn:

                    try:
                        # convert to initital dataset type
                        level_predictions = dataset.from_dataset(level_predictions)
                    except TypeError:
                        raise TypeError('Can not convert prediction dataset type to input features. Set skip_conn=False')
                    dataset = concatenate([level_predictions, dataset])
                else:
                    dataset = level_predictions
            else:
                blended_prediction = self.blender.predict(level_predictions)

        return blended_prediction

    def collect_used_feats(self) -> List[str]:
        """Get feats that automl uses on inference.

        Returns:
            Features names list.

        """
        used_feats = set()

        for lvl in self.levels:
            for pipe in lvl:
                used_feats.update(pipe.used_features)

        used_feats = list(used_feats)

        return used_feats

    def collect_model_stats(self) -> Dict[str, int]:
        """Collect info about models in automl.

        Returns:
            Dict of ``{'Model': n_runtimes}``.

        """
        model_stats = {}

        for lvl in self.levels:
            for pipe in lvl:
                for ml_algo in pipe.ml_algos:
                    model_stats[ml_algo.name] = len(ml_algo.models)

        return model_stats

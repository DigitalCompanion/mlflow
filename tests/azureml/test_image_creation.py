from __future__ import print_function

import sys
import os
import json
import pytest
import mock
from mock import Mock

import pandas as pd
import sklearn.datasets as datasets
import sklearn.linear_model as glm
from click.testing import CliRunner

import mlflow
import mlflow.azureml
import mlflow.azureml.cli
import mlflow.sklearn
from mlflow import pyfunc
from mlflow.exceptions import MlflowException
from mlflow.models import Model
from mlflow.protos.databricks_pb2 import INVALID_PARAMETER_VALUE
from mlflow.tracking.utils import _get_model_log_dir
from mlflow.utils.file_utils import TempDir


pytestmark = pytest.mark.skipif(
        (sys.version_info < (3, 0)),
        reason="Tests require Python 3 to run!")


class AzureMLMocks:

    def __init__(self):
        self.mocks = {
            "register_model": mock.patch("azureml.core.model.Model.register"),
            "get_model_path": mock.patch("azureml.core.model.Model.get_model_path"),
            "create_image": mock.patch("azureml.core.Image.create"),
            "load_workspace": mock.patch("azureml.core.Workspace.get"),
        }

    def __getitem__(self, key):
        return self.mocks[key]

    def __enter__(self):
        for key, mock in self.mocks.items():
            self.mocks[key] = mock.__enter__()
        return self

    def __exit__(self, *args):
        for mock in self.mocks.values():
            mock.__exit__(*args)


def get_azure_workspace():
    # pylint: disable=import-error
    from azureml.core import Workspace
    return Workspace.get("test_workspace")


@pytest.fixture(scope="session")
def sklearn_model():
    iris = datasets.load_iris()
    X = iris.data[:, :2]  # we only take the first two features.
    y = iris.target
    linear_lr = glm.LogisticRegression()
    linear_lr.fit(X, y)
    return linear_lr


@pytest.fixture
def model_path(tmpdir):
    return os.path.join(str(tmpdir), "model")


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_build_image_with_absolute_model_path_calls_expected_azure_routines(
        sklearn_model, model_path):
    mlflow.sklearn.save_model(sk_model=sklearn_model, path=model_path)
    with AzureMLMocks() as aml_mocks:
        workspace = get_azure_workspace()
        mlflow.azureml.build_image(model_path=model_path, workspace=workspace)

        assert aml_mocks["register_model"].call_count == 1
        assert aml_mocks["create_image"].call_count == 1


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_build_image_with_run_relative_model_path_calls_expected_azure_routines(sklearn_model):
    artifact_path = "model"
    with mlflow.start_run():
        mlflow.sklearn.log_model(sk_model=sklearn_model, artifact_path=artifact_path)
        run_id = mlflow.active_run().info.run_uuid

    with AzureMLMocks() as aml_mocks:
        workspace = get_azure_workspace()
        mlflow.azureml.build_image(model_path=artifact_path, run_id=run_id, workspace=workspace)

        assert aml_mocks["register_model"].call_count == 1
        assert aml_mocks["create_image"].call_count == 1


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_synchronous_build_image_awaits_azure_image_creation(sklearn_model, model_path):
    mlflow.sklearn.save_model(sk_model=sklearn_model, path=model_path)
    with AzureMLMocks():
        workspace = get_azure_workspace()
        image, _ = mlflow.azureml.build_image(
                model_path=model_path, workspace=workspace, synchronous=True)
        image.wait_for_creation.assert_called_once()


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_asynchronous_build_image_does_not_await_azure_image_creation(sklearn_model, model_path):
    mlflow.sklearn.save_model(sk_model=sklearn_model, path=model_path)
    with AzureMLMocks():
        workspace = get_azure_workspace()
        image, _ = mlflow.azureml.build_image(
                model_path=model_path, workspace=workspace, synchronous=False)
        image.wait_for_creation.assert_not_called()


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_build_image_registers_model_and_creates_image_with_specified_names(
        sklearn_model, model_path):
    mlflow.sklearn.save_model(sk_model=sklearn_model, path=model_path)
    with AzureMLMocks() as aml_mocks:
        workspace = get_azure_workspace()
        model_name = "MODEL_NAME_1"
        image_name = "IMAGE_NAME_1"
        mlflow.azureml.build_image(
                model_path=model_path, workspace=workspace, model_name=model_name,
                image_name=image_name)

        register_model_call_args = aml_mocks["register_model"].call_args_list
        assert len(register_model_call_args) == 1
        _, register_model_call_kwargs = register_model_call_args[0]
        assert register_model_call_kwargs["model_name"] == model_name

        create_image_call_args = aml_mocks["create_image"].call_args_list
        assert len(create_image_call_args) == 1
        _, create_image_call_kwargs = create_image_call_args[0]
        assert create_image_call_kwargs["name"] == image_name


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_build_image_generates_model_and_image_names_meeting_azureml_resource_naming_requirements(
        sklearn_model, model_path):
    aml_resource_name_max_length = 32

    mlflow.sklearn.save_model(sk_model=sklearn_model, path=model_path)
    with AzureMLMocks() as aml_mocks:
        workspace = get_azure_workspace()
        mlflow.azureml.build_image(model_path=model_path, workspace=workspace)

        register_model_call_args = aml_mocks["register_model"].call_args_list
        assert len(register_model_call_args) == 1
        _, register_model_call_kwargs = register_model_call_args[0]
        called_model_name = register_model_call_kwargs["model_name"]
        assert len(called_model_name) <= aml_resource_name_max_length

        create_image_call_args = aml_mocks["create_image"].call_args_list
        assert len(create_image_call_args) == 1
        _, create_image_call_kwargs = create_image_call_args[0]
        called_image_name = create_image_call_kwargs["name"]
        assert len(called_image_name) <= aml_resource_name_max_length


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_build_image_passes_model_conda_environment_to_azure_image_creation_routine(
        sklearn_model, model_path):
    sklearn_conda_env_text = """\
    name: sklearn-env
    dependencies:
        - scikit-learn
    """
    with TempDir(chdr=True) as tmp:
        sklearn_conda_env_path = tmp.path("conda.yaml")
        with open(sklearn_conda_env_path, "w") as f:
            f.write(sklearn_conda_env_text)

        mlflow.sklearn.save_model(sk_model=sklearn_model, path=model_path,
                                  conda_env=sklearn_conda_env_path)

        # Mock the TempDir.__exit__ function to ensure that the enclosing temporary
        # directory is not deleted
        with AzureMLMocks() as aml_mocks,\
                mock.patch("mlflow.utils.file_utils.TempDir.path") as tmpdir_path_mock,\
                mock.patch("mlflow.utils.file_utils.TempDir.__exit__"):
            def get_mock_path(subpath):
                # Our current working directory is a temporary directory. Therefore, it is safe to
                # directly return the specified subpath.
                return subpath
            tmpdir_path_mock.side_effect = get_mock_path

            workspace = get_azure_workspace()
            mlflow.azureml.build_image(model_path=model_path, workspace=workspace)

            create_image_call_args = aml_mocks["create_image"].call_args_list
            assert len(create_image_call_args) == 1
            _, create_image_call_kwargs = create_image_call_args[0]
            image_config = create_image_call_kwargs["image_config"]
            assert image_config.conda_file is not None
            with open(image_config.conda_file, "r") as f:
                assert f.read() == sklearn_conda_env_text


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_build_image_includes_default_metadata_in_azure_image_and_model_tags(sklearn_model):
    artifact_path = "model"
    with mlflow.start_run():
        mlflow.sklearn.log_model(sk_model=sklearn_model, artifact_path=artifact_path)
        run_id = mlflow.active_run().info.run_uuid
    model_config = Model.load(os.path.join(_get_model_log_dir(artifact_path, run_id), "MLmodel"))

    with AzureMLMocks() as aml_mocks:
        workspace = get_azure_workspace()
        mlflow.azureml.build_image(model_path=artifact_path, run_id=run_id, workspace=workspace)

        register_model_call_args = aml_mocks["register_model"].call_args_list
        assert len(register_model_call_args) == 1
        _, register_model_call_kwargs = register_model_call_args[0]
        called_tags = register_model_call_kwargs["tags"]
        assert called_tags["run_id"] == run_id
        assert called_tags["model_path"] == artifact_path
        assert called_tags["python_version"] ==\
            model_config.flavors[pyfunc.FLAVOR_NAME][pyfunc.PY_VERSION]

        create_image_call_args = aml_mocks["create_image"].call_args_list
        assert len(create_image_call_args) == 1
        _, create_image_call_kwargs = create_image_call_args[0]
        image_config = create_image_call_kwargs["image_config"]
        assert image_config.tags["run_id"] == run_id
        assert image_config.tags["model_path"] == artifact_path
        assert image_config.tags["python_version"] ==\
            model_config.flavors[pyfunc.FLAVOR_NAME][pyfunc.PY_VERSION]


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_build_image_includes_user_specified_tags_in_azure_image_and_model_tags(
        sklearn_model, model_path):
    custom_tags = {
        "User": "Corey",
        "Date": "Today",
        "Other": "Entry",
    }

    mlflow.sklearn.save_model(sk_model=sklearn_model, path=model_path)
    with AzureMLMocks() as aml_mocks:
        workspace = get_azure_workspace()
        mlflow.azureml.build_image(model_path=model_path, workspace=workspace, tags=custom_tags)

        register_model_call_args = aml_mocks["register_model"].call_args_list
        assert len(register_model_call_args) == 1
        _, register_model_call_kwargs = register_model_call_args[0]
        called_tags = register_model_call_kwargs["tags"]
        assert custom_tags.items() <= called_tags.items()

        create_image_call_args = aml_mocks["create_image"].call_args_list
        assert len(create_image_call_args) == 1
        _, create_image_call_kwargs = create_image_call_args[0]
        image_config = create_image_call_kwargs["image_config"]
        assert custom_tags.items() <= image_config.tags.items()


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_build_image_includes_user_specified_description_in_azure_image_and_model_tags(
        sklearn_model, model_path):
    custom_description = "a custom description"

    mlflow.sklearn.save_model(sk_model=sklearn_model, path=model_path)
    with AzureMLMocks() as aml_mocks:
        workspace = get_azure_workspace()
        mlflow.azureml.build_image(
                model_path=model_path, workspace=workspace, description=custom_description)

        register_model_call_args = aml_mocks["register_model"].call_args_list
        assert len(register_model_call_args) == 1
        _, register_model_call_kwargs = register_model_call_args[0]
        assert register_model_call_kwargs["description"] == custom_description

        create_image_call_args = aml_mocks["create_image"].call_args_list
        assert len(create_image_call_args) == 1
        _, create_image_call_kwargs = create_image_call_args[0]
        image_config = create_image_call_kwargs["image_config"]
        assert image_config.description == custom_description


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_build_image_throws_exception_if_model_does_not_contain_pyfunc_flavor(
        sklearn_model, model_path):
    mlflow.sklearn.save_model(sk_model=sklearn_model, path=model_path)
    model_config_path = os.path.join(model_path, "MLmodel")
    model_config = Model.load(model_config_path)
    del model_config.flavors[pyfunc.FLAVOR_NAME]
    model_config.save(model_config_path)

    with AzureMLMocks(), pytest.raises(MlflowException) as exc:
        workspace = get_azure_workspace()
        mlflow.azureml.build_image(model_path=model_path, workspace=workspace)
        assert exc.error_code == INVALID_PARAMETER_VALUE


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_build_image_throws_exception_if_model_python_version_is_less_than_three(
        sklearn_model, model_path):
    mlflow.sklearn.save_model(sk_model=sklearn_model, path=model_path)
    model_config_path = os.path.join(model_path, "MLmodel")
    model_config = Model.load(model_config_path)
    model_config.flavors[pyfunc.FLAVOR_NAME][pyfunc.PY_VERSION] = "2.7.6"
    model_config.save(model_config_path)

    with AzureMLMocks(), pytest.raises(MlflowException) as exc:
        workspace = get_azure_workspace()
        mlflow.azureml.build_image(model_path=model_path, workspace=workspace)
        assert exc.error_code == INVALID_PARAMETER_VALUE


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_build_image_includes_mlflow_home_as_file_dependency_if_specified(
        sklearn_model, model_path):
    def mock_create_dockerfile(output_path, *args, **kwargs):
        # pylint: disable=unused-argument
        with open(output_path, "w") as f:
            f.write("Dockerfile contents")

    mlflow.sklearn.save_model(sk_model=sklearn_model, path=model_path)
    with AzureMLMocks() as aml_mocks, TempDir() as tmp,\
            mock.patch("mlflow.azureml._create_dockerfile") as create_dockerfile_mock:
        create_dockerfile_mock.side_effect = mock_create_dockerfile

        # Write a mock `setup.py` file to the mlflow home path so that it will be recognized
        # as a viable MLflow source directory during the image build process
        mlflow_home = tmp.path()
        with open(os.path.join(mlflow_home, "setup.py"), "w") as f:
            f.write("setup instructions")

        workspace = get_azure_workspace()
        mlflow.azureml.build_image(
                model_path=model_path, workspace=workspace, mlflow_home=mlflow_home)

        assert len(create_dockerfile_mock.call_args_list) == 1
        _, create_dockerfile_kwargs = create_dockerfile_mock.call_args_list[0]
        # The path to MLflow that is referenced by the Docker container may differ from the
        # user-specified `mlflow_home` path if the directory is copied before image building
        # for safety
        dockerfile_mlflow_path = create_dockerfile_kwargs["mlflow_path"]

        create_image_call_args = aml_mocks["create_image"].call_args_list
        assert len(create_image_call_args) == 1
        _, create_image_call_kwargs = create_image_call_args[0]
        image_config = create_image_call_kwargs["image_config"]
        assert dockerfile_mlflow_path in image_config.dependencies


def test_execution_script_init_method_attempts_to_load_correct_azure_ml_model(
        sklearn_model, model_path):
    mlflow.sklearn.save_model(sk_model=sklearn_model, path=model_path)

    model_name = "test_model_name"
    model_version = 1

    model_mock = Mock()
    model_mock.name = model_name
    model_mock.version = model_version

    with TempDir() as tmp:
        execution_script_path = tmp.path("dest")
        mlflow.azureml._create_execution_script(
                output_path=execution_script_path, azure_model=model_mock)

        with open(execution_script_path, "r") as f:
            execution_script = f.read()

    # Define the `init` and `score` methods contained in the execution script
    # pylint: disable=exec-used
    exec(execution_script, globals())
    with AzureMLMocks() as aml_mocks:
        aml_mocks["get_model_path"].side_effect = lambda *args, **kwargs: model_path
        # Execute the `init` method of the execution script.
        # pylint: disable=undefined-variable
        init()

        assert aml_mocks["get_model_path"].call_count == 1
        get_model_path_call_args = aml_mocks["get_model_path"].call_args_list
        assert len(get_model_path_call_args) == 1
        _, get_model_path_call_kwargs = get_model_path_call_args[0]
        assert get_model_path_call_kwargs["model_name"] == model_name
        assert get_model_path_call_kwargs["version"] == model_version


def test_execution_script_run_method_scores_pandas_dataframes_successfully(
        sklearn_model, model_path):
    mlflow.sklearn.save_model(sk_model=sklearn_model, path=model_path)

    model_mock = Mock()
    model_mock.name = "model_name"
    model_mock.version = 1

    with TempDir() as tmp:
        execution_script_path = tmp.path("dest")
        mlflow.azureml._create_execution_script(
                output_path=execution_script_path, azure_model=model_mock)

        with open(execution_script_path, "r") as f:
            execution_script = f.read()

    # Define the `init` and `score` methods contained in the execution script
    # pylint: disable=exec-used
    exec(execution_script, globals())
    with AzureMLMocks() as aml_mocks:
        aml_mocks["get_model_path"].side_effect = lambda *args, **kwargs: model_path
        # Execute the `init` method of the execution script and load the sklearn model from the
        # mocked path
        # pylint: disable=undefined-variable
        init()

        # Invoke the `run` method of the execution script with sample input data and verify that
        # reasonable output data is produced
        input_data = datasets.load_iris().data[:, :2]
        # pylint: disable=undefined-variable
        output_data = run(pd.DataFrame(data=input_data).to_json(orient="records"))
        assert len(output_data) == len(input_data)


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_cli_build_image_with_absolute_model_path_calls_expected_azure_routines(
        sklearn_model, model_path):
    mlflow.sklearn.save_model(sk_model=sklearn_model, path=model_path)
    with AzureMLMocks() as aml_mocks:
        result = CliRunner(env={"LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}).invoke(
                mlflow.azureml.cli.commands,
                [
                    'build-image',
                    '-m', model_path,
                    '-w', "test_workspace",
                    '-i', "image_name",
                    '-n', "model_name",
                ])
        assert result.exit_code == 0

        assert aml_mocks["register_model"].call_count == 1
        assert aml_mocks["create_image"].call_count == 1
        assert aml_mocks["load_workspace"].call_count == 1


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_cli_build_image_with_run_relative_model_path_calls_expected_azure_routines(sklearn_model):
    artifact_path = "model"
    with mlflow.start_run():
        mlflow.sklearn.log_model(sk_model=sklearn_model, artifact_path=artifact_path)
        run_id = mlflow.active_run().info.run_uuid

    with AzureMLMocks() as aml_mocks:
        result = CliRunner(env={"LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}).invoke(
                mlflow.azureml.cli.commands,
                [
                    'build-image',
                    '-m', artifact_path,
                    '-r', run_id,
                    '-w', 'test_workspace',
                    '-i', 'image_name',
                    '-n', 'model_name',
                ])
        assert result.exit_code == 0

        assert aml_mocks["register_model"].call_count == 1
        assert aml_mocks["create_image"].call_count == 1
        assert aml_mocks["load_workspace"].call_count == 1


@mock.patch("mlflow.azureml.mlflow_version", "0.7.0")
def test_cli_build_image_parses_and_includes_user_specified_tags_in_azureml_image_and_model_tags(
        sklearn_model):
    custom_tags = {
        "User": "Corey",
        "Date": "Today",
        "Other": "Entry",
    }

    artifact_path = "model"
    with mlflow.start_run():
        mlflow.sklearn.log_model(sk_model=sklearn_model, artifact_path=artifact_path)
        run_id = mlflow.active_run().info.run_uuid

    with AzureMLMocks() as aml_mocks:
        result = CliRunner(env={"LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}).invoke(
                mlflow.azureml.cli.commands,
                [
                    'build-image',
                    '-m', artifact_path,
                    '-r', run_id,
                    '-w', 'test_workspace',
                    '-t', json.dumps(custom_tags),
                 ])
        assert result.exit_code == 0

        register_model_call_args = aml_mocks["register_model"].call_args_list
        assert len(register_model_call_args) == 1
        _, register_model_call_kwargs = register_model_call_args[0]
        called_tags = register_model_call_kwargs["tags"]
        assert custom_tags.items() <= called_tags.items()

        create_image_call_args = aml_mocks["create_image"].call_args_list
        assert len(create_image_call_args) == 1
        _, create_image_call_kwargs = create_image_call_args[0]
        image_config = create_image_call_kwargs["image_config"]
        assert custom_tags.items() <= image_config.tags.items()
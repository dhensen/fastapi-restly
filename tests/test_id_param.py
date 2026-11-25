"""``id_param`` names the id in the path of the item routes."""

from collections.abc import Callable, Iterator
from typing import Annotated, Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Path
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

import fastapi_restly as fr
from fastapi_restly._routes import iter_routes
from fastapi_restly.exc import RestlyConfigurationError
from fastapi_restly.testing import RestlyTestClient
from fastapi_restly.views import IDType, ViewResponse

from ._models import SharedBase
from .conftest import create_tables


class ProjectSchema(fr.IDSchema):
    name: str


@pytest.fixture
def Project() -> type:
    class Project(fr.IDMixin, SharedBase):
        name: Mapped[str]

    return Project


@pytest.fixture(params=[fr.RestView, fr.AsyncRestView], ids=["sync", "async"])
def view_base(request: pytest.FixtureRequest) -> Iterator[type]:
    if request.param is fr.RestView:
        request.getfixturevalue("sync_db")
    yield request.param


@pytest.fixture
def make_app(
    request: pytest.FixtureRequest, view_base: type, Project: type
) -> Callable[..., tuple[FastAPI, RestlyTestClient]]:
    def make(**attrs: Any) -> tuple[FastAPI, RestlyTestClient]:
        view = type(
            "ProjectView",
            (view_base,),
            {
                "prefix": "/projects",
                "model": Project,
                "schema": ProjectSchema,
                "id_param": "project_id",
                **attrs,
            },
        )
        app = FastAPI()
        fr.include_view(app, view)
        if view_base is fr.RestView:
            engine, _ = request.getfixturevalue("sync_db")
            SharedBase.metadata.create_all(engine)
        else:
            create_tables()
        return app, RestlyTestClient(app)

    return make


def _parameters(app: FastAPI, path: str, method: str) -> list[dict[str, Any]]:
    return app.openapi()["paths"][path][method]["parameters"]


def test_item_routes_use_the_name_in_openapi(make_app):
    app, _ = make_app()
    paths = app.openapi()["paths"]
    assert "/projects/{project_id}" in paths
    assert "/projects/{id}" not in paths
    for method in ("get", "patch", "delete"):
        (parameter,) = _parameters(app, "/projects/{project_id}", method)
        assert parameter["name"] == "project_id"
        assert parameter["in"] == "path"
        assert parameter["schema"]["type"] == "integer"
    operation = paths["/projects/{project_id}"]["get"]
    assert operation["operationId"] == "projects_get"


def test_item_routes_answer(make_app):
    _, client = make_app()
    created = client.post("/projects", json={"name": "Apollo"}).json()
    url = f"/projects/{created['id']}"
    assert client.get(url).json() == created
    updated = client.patch(url, json={"name": "Gemini"}).json()
    assert updated["name"] == "Gemini"
    client.delete(url, assert_status_code=204)
    client.get(url, assert_status_code=404)
    # the int converter stays: a value that is not a number does not match
    client.get("/projects/abc", assert_status_code=404)


def test_custom_route_with_id_or_with_the_name(make_app):
    def stats(self, id: IDType) -> dict[str, Any]:
        return {"id": id}

    def summary(self, id: IDType) -> dict[str, Any]:
        return {"id": id}

    app, client = make_app(
        stats=fr.get("/{id}/stats")(stats),
        summary=fr.get("/{project_id}/summary")(summary),
    )
    paths = app.openapi()["paths"]
    assert "/projects/{project_id}/stats" in paths
    assert "/projects/{project_id}/summary" in paths
    assert client.get("/projects/3/stats").json() == {"id": 3}
    assert client.get("/projects/3/summary").json() == {"id": 3}


def test_an_override_of_an_endpoint_keeps_the_parameter_id(make_app, view_base):
    if view_base is fr.AsyncRestView:

        async def get_one_endpoint(self, id: IDType) -> ViewResponse:
            return await self.handle_get_one(id)

    else:

        def get_one_endpoint(self, id: IDType) -> ViewResponse:
            return self.handle_get_one(id)

    app, client = make_app(get_one_endpoint=fr.get("/{id}")(get_one_endpoint))
    created = client.post("/projects", json={"name": "Apollo"}).json()
    assert client.get(f"/projects/{created['id']}").json() == created
    (parameter,) = _parameters(app, "/projects/{project_id}", "get")
    assert parameter["name"] == "project_id"


def test_path_options_of_the_annotation_stay(make_app):
    def check(self, id: Annotated[IDType, Path(ge=5)]) -> dict[str, Any]:
        return {"id": id}

    app, client = make_app(check=fr.get("/{id}/check")(check))
    (parameter,) = _parameters(app, "/projects/{project_id}/check", "get")
    assert parameter["name"] == "project_id"
    assert parameter["schema"]["minimum"] == 5
    assert client.get("/projects/7/check").json() == {"id": 7}
    error = client.get("/projects/2/check", assert_status_code=422).json()
    assert error["detail"][0]["loc"] == ["path", "project_id"]


def test_the_default_keeps_id(view_base, Project):
    view = type(
        "ProjectView",
        (view_base,),
        {"prefix": "/projects", "model": Project, "schema": ProjectSchema},
    )
    app = FastAPI()
    fr.include_view(app, view)
    (parameter,) = _parameters(app, "/projects/{id}", "get")
    assert parameter["name"] == "id"


def test_a_subclass_can_set_another_name(view_base, Project):
    base = type(
        "BaseProjectView",
        (view_base,),
        {"model": Project, "schema": ProjectSchema, "id_param": "project_id"},
    )
    first = type("FirstView", (base,), {"prefix": "/first"})
    second = type("SecondView", (base,), {"prefix": "/second", "id_param": "key"})
    app = FastAPI()
    fr.include_view(app, first)
    fr.include_view(app, second)
    paths = app.openapi()["paths"]
    assert "/first/{project_id}" in paths
    assert "/second/{key}" in paths


def test_uuid_ids_keep_the_converter(view_base):
    class Ticket(SharedBase):
        __tablename__ = "id_param_ticket"
        id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default_factory=uuid4)
        name: Mapped[str]

    class TicketSchema(fr.BaseSchema):
        id: fr.ReadOnly[UUID]
        name: str

    view = type(
        "TicketView",
        (view_base,),
        {
            "prefix": "/tickets",
            "model": Ticket,
            "schema": TicketSchema,
            "id_param": "ticket_id",
        },
    )
    app = FastAPI()
    fr.include_view(app, view)
    paths = {route.path for route in iter_routes(app.routes)}
    assert "/tickets/{ticket_id:uuid}" in paths


@pytest.mark.parametrize("bad", ["", "project-id", "{id}", 5])
def test_a_bad_name_is_refused(view_base, Project, bad):
    with pytest.raises(RestlyConfigurationError, match="id_param"):
        view = type(
            "ProjectView",
            (view_base,),
            {
                "prefix": "/projects",
                "model": Project,
                "schema": ProjectSchema,
                "id_param": bad,
            },
        )
        fr.include_view(FastAPI(), view)


def test_an_id_too_large_for_the_column_names_the_parameter(make_app):
    _, client = make_app()
    error = client.get(f"/projects/{2**70}", assert_status_code=422).json()
    assert error["detail"][0]["loc"] == ["path", "project_id"]


def test_the_put_route_of_react_admin_uses_the_name(Project):
    view = type(
        "ProjectView",
        (fr.AsyncReactAdminView,),
        {
            "prefix": "/projects",
            "model": Project,
            "schema": ProjectSchema,
            "id_param": "project_id",
        },
    )
    app = FastAPI()
    fr.include_view(app, view)
    (parameter,) = _parameters(app, "/projects/{project_id}", "put")
    assert parameter["name"] == "project_id"


def test_a_name_that_the_prefix_has_is_refused(view_base, Project):
    view = type(
        "TaskView",
        (view_base,),
        {
            "prefix": "/projects/{project_id}/tasks",
            "model": Project,
            "schema": ProjectSchema,
            "id_param": "project_id",
        },
    )
    with pytest.raises(RestlyConfigurationError, match="prefix"):
        fr.include_view(FastAPI(), view)

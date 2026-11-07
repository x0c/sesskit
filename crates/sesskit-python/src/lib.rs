//! Python boundary only; native algorithms live in the shared Rust library.
use pyo3::prelude::*;
use pyo3::types::PyString;

#[pyfunction]
fn project_json_many(
    py: Python<'_>,
    records: Vec<Py<PyString>>,
    profile: String,
) -> PyResult<Vec<Option<String>>> {
    // Keep strong references while detached, but borrow immutable UTF-8 data
    // instead of copying large history payloads across the FFI boundary.
    let inputs: Vec<&str> = records
        .iter()
        .map(|raw| raw.bind(py).to_str())
        .collect::<PyResult<_>>()?;
    Ok(py.detach(move || {
        inputs
            .iter()
            .map(|raw| sesskit_stats::projection::project_json(raw, &profile))
            .collect()
    }))
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("PROTOCOL_VERSION", 1)?;
    module.add_function(wrap_pyfunction!(project_json_many, module)?)?;
    Ok(())
}

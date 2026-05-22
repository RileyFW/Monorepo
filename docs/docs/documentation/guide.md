# Overview

Documentation is powered by [MkDocs](https://www.mkdocs.org/) with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/), hosted on [Github Pages](https://pages.github.com/). [Pipenv](https://pipenv.pypa.io/en/latest/) is the package manager the documentation uses.

## Setup
For developers, an MkDocs setup is included as part of the dev install script.

!!! note
    The dev install script requires a .env file for any environment variables needed. To create a .env file, copy ".example.env" and make any necessary changes.

## Updating
Updating documentation is as simple as updating Markdown files, all contained within the `docs` folder in the Monorepo. 

To preview your chages, follow these steps:

Starting in the root directory, navigate to the outermost docs directory.

```sh
cd docs
```

Create and activate a fresh virtual environment.

```sh
python3 -m venv venv
source venv/bin/activate
```

Install the required dependencies.

```sh
pip install --upgrade pip
pip install -r requirements.txt
```

Start up the local dev server to preview changes.

```sh
mkdocs serve
```

## Deploying

With Github Actions, updates to the `main` branch will automatically build and deploy the static site to the `gh-pages` branch.

To deploy manually, run
```sh
pipenv run mkdocs gh-deploy --force
```

!!! warning
    This updates the `gh-pages` branch with the static site. Keep in mind, if there are unpublished changes, it will display those changes as well, so beware!

To preview what files will be generated and published, this generates the static website files under `docs/site/`:
```sh
pipenv run mkdocs build
```

## Notes

There are many [MkDocs Plugins](https://github.com/mkdocs/catalog) that may be useful while updating documentation, so consider them as this documentation evolves! [More information about plugins here](https://www.mkdocs.org/dev-guide/plugins/).

The Github Action for automatic deployment was found [here](https://squidfunk.github.io/mkdocs-material/publishing-your-site/#with-github-actions), slightly changed.

If time allows for it, consider looking at [Sphinx](https://www.sphinx-doc.org/en/master/) instead, for documentation. It seems more advanced, with more features and apparently better code integration with documentation, but may be harder to set up and learn.
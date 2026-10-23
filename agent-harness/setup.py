from setuptools import setup, find_namespace_packages

setup(name='cli-anything-invoice-assistant', version='0.1.0',
      description='Direct CLI for the existing local invoice assistant API',
      packages=find_namespace_packages(include=['cli_anything.*'], exclude=['cli_anything.invoice_assistant.tests*']),
      package_data={'cli_anything.invoice_assistant': ['README.md', 'skills/*.md']},
      python_requires='>=3.12', install_requires=['click>=8', 'prompt-toolkit>=3'],
      entry_points={'console_scripts': [
          'invoice-assistant=cli_anything.invoice_assistant.invoice_assistant_cli:main',
          'cli-anything-invoice-assistant=cli_anything.invoice_assistant.invoice_assistant_cli:main']})

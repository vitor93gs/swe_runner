# SWE Runner Project

## Overview
This project provides a suite of tools for running Software Engineering (SWE) tasks in isolated Docker environments. It consists of three main components that work together to process tasks defined in Google Sheets/CSV files and execute them using the SWE agent.

## Components

### 1. `run_batch.py`
The main entry point for batch processing tasks. This script:
- Reads task definitions from a Google Sheet or local CSV file
- Creates individual task folders with required assets
- Executes each task using the SWE runner in isolated environments

Usage:
```bash
python run_batch.py --sheet <sheet_url_or_path> [options]

Options:
  --sheet          Required. Google Sheets URL or CSV path containing task definitions
  --tasks-dir      Folder for task subfolders (default: tasks)
  --model          LiteLLM model string (default: gemini/gemini-2.5-pro)
  --limit          Process only first N tasks (0 = all)
  --only-task-ids  Comma-separated task IDs to include
  --swe-runner-path Path to swe_runner.py
```

### 2. `task_prep.py`
Handles task preparation and file management:
- Downloads files from Google Drive
- Creates task-specific directories
- Manages Dockerfile and test assets
- Supports both local files and remote (Google Drive) resources

Key Features:
- Google Sheets integration
- Google Drive file downloading
- Automatic file organization
- Support for test patches and commands

### 3. `swe_runner.py`
The core execution engine that:
- Builds and manages Docker images
- Sets up SWE agent environments
- Handles model API keys and configurations
- Manages workspace directories and Git repositories

Features:
- Docker overlay system for dependencies
- Multi-OS support (Debian, Alpine, RHEL)
- Automatic Python environment setup
- Git repository management
- Cost and API call limiting

## Task Structure
Tasks are defined in a Google Sheet/CSV with the following columns:
- `task_id`: Unique identifier for the task
- `updated_issue_description`: Task description (saved as task.md)
- `dockerfile`: Path or URL to Dockerfile
- `test_command`: Command for testing
- `test_patch`: Optional test patch file

## Directory Structure
```
project/
├── tasks/                    # Generated task folders
│   └── task_id_<N>/         # Individual task folders
│       ├── Dockerfile       # Task-specific Dockerfile
│       ├── task.md         # Task description
│       ├── test_command.txt # Test command specification
│       └── test_patch.tar  # Optional test patch
├── trajectories/            # Task execution outputs
├── run_batch.py            # Main batch runner
├── swe_runner.py          # SWE execution engine
└── task_prep.py           # Task preparation utilities
```

## Prerequisites
- Python 3.x
- Docker
- Git
- Access to a gemini API key

## Environment Variables
Required environment variables (in `.env.sweagent`):
- For Gemini models: `GEMINI_API_KEY` or `GOOGLE_API_KEY`

## Installation
1. Clone the repository
2. Ensure Docker is installed and running
3. Create `.env.sweagent` with required API keys
4. Install required Python packages:
   ```bash
   pip install requests gdown
   ```

## Example Usage
1. Create a Google Sheet with task definitions
2. Run the batch processor:
   ```bash
   python run_batch.py --sheet "https://docs.google.com/spreadsheets/d/..." \
                      --model "gemini/gemini-2.5-pro" \
   ```

## Docker Integration
The system uses a two-layer Docker approach:
1. Base image: Contains the core project/repository
2. Overlay image: Adds runtime dependencies (Python, Git, etc.)

### Supported OS Families
- Debian/Ubuntu
- Alpine Linux
- RHEL/Fedora/CentOS/Rocky
- Generic fallback for unknown bases

## Error Handling
- Robust error handling for file downloads
- Fallback mechanisms for dependency installation
- Graceful handling of missing Git repositories
- API key validation

## Limitations
- Requires Docker for execution
- Google Drive downloads may require authentication
- Some features require active internet connection

## Contributing
1. Fork the repository
2. Create your feature branch
3. Commit your changes
4. Push to the branch
5. Create a new Pull Request
# Use an official, lightweight Python image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file and install the dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your Python code into the container
COPY main.py .

# Azure App Service routes traffic to port 80 by default
EXPOSE 80

# Command to run the FastAPI server on port 80
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]
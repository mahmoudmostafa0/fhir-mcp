# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Install PyPDF2 for PDF text extraction
RUN pip install --no-cache-dir PyPDF2

# Copy the rest of the application code
COPY . .

# Make port 8080 available to the world outside this container
EXPOSE 8080

# Define environment variables
ENV FHIR_BASE_URL=https://hapi-development.up.railway.app/fhir
ENV FHIR_AUTH_TOKEN=""

# Run the application when the container launches
CMD ["python", "fhir_mcp_server.py"]

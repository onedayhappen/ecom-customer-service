FROM node:20-alpine AS build

WORKDIR /frontend
COPY package*.json ./
RUN npm ci
COPY . ./

ARG VITE_API_BASE_URL=http://localhost:8010
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
ARG VITE_TASK_PIPELINE_ENABLED=false
ENV VITE_TASK_PIPELINE_ENABLED=${VITE_TASK_PIPELINE_ENABLED}
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /frontend/dist /usr/share/nginx/html
EXPOSE 80

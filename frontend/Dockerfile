ARG NODE_IMAGE=node:20-alpine
ARG NGINX_IMAGE=nginx:1.27-alpine
FROM ${NODE_IMAGE} AS build

WORKDIR /frontend
COPY package*.json ./
RUN npm ci
COPY . ./

ARG VITE_API_BASE_URL=http://localhost:8010
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm run build

FROM ${NGINX_IMAGE}
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /frontend/dist /usr/share/nginx/html
EXPOSE 80
